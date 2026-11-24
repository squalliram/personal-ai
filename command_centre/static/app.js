"use strict";
(() => {
  const SECTIONS = [
    ["overview", "Overview"], ["inbox", "Inbox"], ["my-day", "My Day"], ["tasks", "Tasks"], ["calendar", "Calendar"],
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
  // Audit pages are always fetched fresh (never cached); the page and filter survive Settings re-renders.
  const auditView = { page: 1, action: "" };
  let signInCount = 0; // changes only when the session ends, unlike the cache epoch
  let sessionsNotice = ""; // last sign-out failure; kept across re-renders until the next attempt or sign-out
  const $ = (id) => document.getElementById(id);

  // Display preferences live in this browser only; they never reach the server and change no security setting.
  const PREFS_KEY = "jarvis.preferences";
  const AUDIT_PAGE_SIZES = [10, 25, 50, 100];
  const PREF_DEFAULTS = { startPage: "overview", vault: true, voiceGreeting: true, auditPageSize: 25 };
  function prefs() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") || {}; } catch { saved = {}; }
    return {
      startPage: SECTIONS.some(([id]) => id === saved.startPage) ? saved.startPage : PREF_DEFAULTS.startPage,
      vault: typeof saved.vault === "boolean" ? saved.vault : PREF_DEFAULTS.vault,
      voiceGreeting: typeof saved.voiceGreeting === "boolean" ? saved.voiceGreeting : PREF_DEFAULTS.voiceGreeting,
      auditPageSize: AUDIT_PAGE_SIZES.includes(saved.auditPageSize) ? saved.auditPageSize : PREF_DEFAULTS.auditPageSize,
    };
  }
  function savePrefs(update) {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify({ ...prefs(), ...update })); return true; } catch { return false; }
  }

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
    const form = opts.body instanceof FormData || opts.body instanceof Blob;
    if (opts.body instanceof Blob) headers["Content-Type"] = "application/octet-stream";
    else if (opts.body !== undefined && !form) headers["Content-Type"] = "application/json";
    if (opts.method && opts.method !== "GET" && csrf) headers["X-CSRF-Token"] = csrf;
    let response;
    try {
      response = await fetch(path, {
        method: opts.method || "GET", headers, credentials: "same-origin", cache: "no-store",
        body: opts.body === undefined || form ? opts.body : JSON.stringify(opts.body), signal: opts.signal,
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
    signInCount += 1;
    holdFailures.clear();
    sessionsNotice = "";
    clearCache();
    auditView.page = 1; auditView.action = "";
    $("view").replaceChildren();
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
    refreshPasskeyOptions();
  }

  function syncTopbarHeight() {
    const height = document.querySelector(".topbar").offsetHeight;
    if (height) document.documentElement.style.setProperty("--topbar-h", `${height}px`);
  }

  function showApp(info) {
    session = info; csrf = info.csrf_token;
    $("login-view").hidden = true;
    $("app-view").hidden = false;
    syncTopbarHeight();
    $("permission-badge").textContent = `Permission: ${info.permission.toUpperCase()}`;
    const start = prefs().startPage;
    if (!location.hash && start !== "overview") history.replaceState(null, "", `#${start}`);
    clearCache();
    render().then(prefetch);
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
    if (result.ok) granted(result.data);
    else $("login-status").textContent = LOGIN_ERRORS[result.data && result.data.error] || "Sign-in failed.";
  }

  function granted(data) {
    stopListening();
    const { vault, voiceGreeting } = prefs();
    if (vault) playVault("unlock");
    if (voiceGreeting) speak(VAULT.unlock.caption, reducedMotion() || !vault ? 0 : VAULT_GRANTED_MS);
    showApp(data);
  }

  // Passkeys ---------------------------------------------------------------------------------
  const b64 = {
    encode(buffer) {
      let text = "";
      for (const byte of new Uint8Array(buffer)) text += String.fromCharCode(byte);
      return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    },
    decode(text) {
      const raw = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4));
      return Uint8Array.from(raw, (c) => c.charCodeAt(0));
    },
  };
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const passkeysSupported = () => Boolean(window.PublicKeyCredential && navigator.credentials);
  const OPTIONS_MAX_AGE_MS = 2 * 60 * 1000;
  let loginOptions = null;

  // Options are fetched ahead so the passkey prompt opens straight from the tap; Safari only
  // shows it while the tap still counts as a user gesture.
  async function prefetchLoginOptions() {
    const result = await api("api/v1/passkeys/login/options", { method: "POST", body: {} });
    loginOptions = result.ok ? { ...result.data, fetched: Date.now() } : null;
    return result;
  }

  async function refreshPasskeyOptions() {
    $("passkey-options").hidden = true;
    if (!passkeysSupported()) return;
    const result = await api("api/v1/passkeys/status");
    if (!result.ok || !result.data.available || !result.data.registered || session) return;
    $("passkey-options").hidden = false;
    $("hello-jarvis").hidden = !SpeechRecognition;
    await prefetchLoginOptions();
  }

  async function passkeySignIn() {
    stopListening();
    let options = loginOptions;
    loginOptions = null;
    if (!options || Date.now() - options.fetched > OPTIONS_MAX_AGE_MS) {
      const result = await prefetchLoginOptions();
      if (!result.ok) {
        $("login-status").textContent = LOGIN_ERRORS[result.data && result.data.error] || "Passkey sign-in is unavailable.";
        return;
      }
      options = loginOptions;
      loginOptions = null;
    }
    $("login-status").textContent = "Confirm it's you with your passkey…";
    let credential;
    try {
      credential = await navigator.credentials.get({ publicKey: {
        challenge: b64.decode(options.challenge), rpId: options.rp_id,
        timeout: Math.max(30000, options.timeout - (Date.now() - options.fetched) - 5000),
        userVerification: options.user_verification, allowCredentials: [] } });
    } catch (error) {
      credential = null;
      $("login-status").textContent = error && error.name === "NotAllowedError"
        ? "Passkey sign-in was cancelled. Tap “Sign in with passkey” to try again."
        : "This device could not use a passkey.";
    }
    prefetchLoginOptions();
    if (!credential) return;
    await pendingLogout;
    const response = credential.response;
    const result = await api("api/v1/passkeys/login", { method: "POST", body: {
      credential_id: b64.encode(credential.rawId), client_data_json: b64.encode(response.clientDataJSON),
      authenticator_data: b64.encode(response.authenticatorData), signature: b64.encode(response.signature),
      remember: $("remember").checked } });
    if (result.ok) granted(result.data);
    else if (result.data && result.data.error === "invalid_credentials") $("login-status").textContent = "That passkey was not accepted.";
    else $("login-status").textContent = LOGIN_ERRORS[result.data && result.data.error] || "Passkey sign-in failed.";
  }

  // "Hello Jarvis" only starts the passkey prompt: the device's Face ID, Touch ID or PIN is
  // what signs you in, so a recording or cloned voice cannot.
  const WAKE_PHRASE = /\b(hello|hey|hi)\W*jarvis\b/i;
  const VOICE_ERRORS = {
    "not-allowed": "Microphone access is blocked for this site. Allow it from the padlock in the address bar.",
    "service-not-allowed": "Microphone access is blocked for this site. Allow it from the padlock in the address bar.",
    "audio-capture": "No microphone was found on this device.",
    "network": "Speech recognition needs an internet connection. Try again.",
    "no-speech": "Jarvis did not hear anything. Try again, closer to the microphone.",
  };
  const VOICE_UNSUPPORTED = "Voice needs Chrome, Edge or Safari. Firefox has no speech recognition.";
  const voiceError = (code, fallback) => VOICE_ERRORS[code] || fallback;
  let recognition = null;
  function stopListening() {
    if (recognition) {
      const active = recognition;
      recognition = null;
      active.abort();
    }
    $("hello-jarvis").setAttribute("aria-pressed", "false");
    $("hello-jarvis").classList.remove("listening");
  }

  function helloJarvis() {
    if (recognition) { stopListening(); $("login-status").textContent = ""; return; }
    const listener = new SpeechRecognition();
    listener.lang = "en-GB";
    listener.interimResults = true;
    listener.maxAlternatives = 3;
    let heard = false;
    listener.onresult = (event) => {
      if (recognition !== listener || heard) return;
      heard = Array.from(event.results).some((result) => Array.from(result).some((alt) => WAKE_PHRASE.test(alt.transcript)));
      if (!heard) return;
      stopListening();
      speak("Hello. Confirm it's you.");
      passkeySignIn();
    };
    listener.onerror = (event) => {
      if (recognition !== listener) return;
      stopListening();
      $("login-status").textContent = voiceError(event.error, "Jarvis could not hear you. Try again.");
    };
    listener.onend = () => {
      if (recognition !== listener) return;
      stopListening();
      $("login-status").textContent = "Jarvis did not hear “Hello Jarvis”. Try again.";
    };
    recognition = listener;
    $("hello-jarvis").setAttribute("aria-pressed", "true");
    $("hello-jarvis").classList.add("listening");
    $("login-status").textContent = "Listening… say “Hello Jarvis”.";
    try { listener.start(); } catch {
      stopListening();
      $("login-status").textContent = VOICE_UNSUPPORTED;
    }
  }

  function deviceName() {
    const agent = navigator.userAgent;
    if (/iPhone/.test(agent)) return "iPhone";
    if (/iPad/.test(agent)) return "iPad";
    if (/Android/.test(agent)) return "Android phone";
    if (/Macintosh|Mac OS X/.test(agent)) return navigator.maxTouchPoints > 1 ? "iPad" : "Mac";
    if (/Windows/.test(agent)) return "Windows PC";
    return "This device";
  }

  async function passkeyCard() {
    const data = await load("api/v1/passkeys");
    if (!data) return null;
    if (!data.available) return card("Passkeys", muted("Passkeys are not available on this server yet, so sign in with your passphrase."));
    const notice = el("p", { role: "status", "aria-live": "polite" });
    const removeButton = (item) => {
      const button = el("button", { type: "button", "aria-label": `Remove passkey ${item.label}` }, "Remove");
      button.addEventListener("click", async () => {
        button.disabled = true;
        const removed = await api("api/v1/passkeys/remove", { method: "POST", body: { credential_id: item.id } });
        if (removed.ok) { clearCache(); render(); } else { button.disabled = false; notice.textContent = "The passkey could not be removed."; }
      });
      return button;
    };
    const finish = el("button", { type: "button", class: "primary", hidden: "" }, "Create passkey");
    // Keep references: render() may detach the form while a passkey prompt is open.
    const passwordInput = el("input", { id: "passkey-password", type: "password", autocomplete: "current-password", required: "", maxlength: "1024" });
    const labelInput = el("input", { id: "passkey-label", type: "text", maxlength: "60", value: deviceName() });
    const form = el("form", { class: "passkey-add" },
      el("input", { type: "text", name: "username", autocomplete: "username", value: "jarvis", hidden: "" }),
      el("label", { for: "passkey-password" }, "Passphrase"), passwordInput,
      el("label", { for: "passkey-label" }, "Name this device"), labelInput,
      el("button", { type: "submit" }, "Add a passkey on this device"), finish);
    let pending = null;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      finish.hidden = true;
      if (!passkeysSupported()) { notice.textContent = "This browser does not support passkeys."; return; }
      const password = passwordInput.value;
      if (!password) { notice.textContent = "Enter your passphrase first."; return; }
      notice.textContent = "Checking your passphrase…";
      const options = await api("api/v1/passkeys/register/options", { method: "POST", body: { password } });
      passwordInput.value = "";
      if (!options.ok) {
        notice.textContent = options.data && options.data.error === "invalid_credentials"
          ? "That passphrase was not accepted." : LOGIN_ERRORS[options.data && options.data.error] || "Passkey setup could not start.";
        return;
      }
      pending = { ...options.data, fetched: Date.now() };
      finish.hidden = false;
      finish.focus();
      notice.textContent = "Passphrase accepted. Select “Create passkey” and confirm with Face ID, Touch ID or your device PIN.";
    });
    finish.addEventListener("click", async () => {
      const o = pending;
      if (!o) return;
      pending = null;
      finish.hidden = true;
      const remaining = o.timeout - (Date.now() - o.fetched) - 5000;
      if (remaining < 30000) { notice.textContent = "That took too long. Enter your passphrase again."; return; }
      let credential;
      try {
        credential = await navigator.credentials.create({ publicKey: {
          challenge: b64.decode(o.challenge), rp: { id: o.rp_id, name: o.rp_name },
          user: { id: b64.decode(o.user_id), name: o.user_name, displayName: o.user_display_name },
          pubKeyCredParams: [{ type: "public-key", alg: -7 }, { type: "public-key", alg: -257 }],
          authenticatorSelection: { residentKey: "required", requireResidentKey: true, userVerification: "required" },
          excludeCredentials: o.exclude_credentials.map((id) => ({ type: "public-key", id: b64.decode(id) })),
          attestation: "none", timeout: remaining } });
      } catch (error) {
        notice.textContent = error && error.name === "InvalidStateError"
          ? "This device already has a Jarvis passkey." : "Passkey setup was cancelled.";
        return;
      }
      const saved = await api("api/v1/passkeys/register", { method: "POST", body: {
        client_data_json: b64.encode(credential.response.clientDataJSON),
        attestation_object: b64.encode(credential.response.attestationObject), label: labelInput.value } });
      if (saved.ok) { clearCache(); render(); }
      else notice.textContent = "The passkey could not be saved.";
    });
    return card("Passkeys",
      muted("Sign in with Face ID, Touch ID, Windows Hello or your phone instead of the passphrase. Passkeys saved to iCloud Keychain or Google Password Manager work on your other devices, and on any other computer you can scan a QR code with your phone. On the sign-in screen, say “Hello Jarvis” to open the passkey prompt."),
      table([["Passkey", (r) => r.label], ["Added", (r) => when(r.created_at)],
        ["Last used", (r) => (r.last_used_at ? when(r.last_used_at) : "Never")], ["", removeButton]], data.passkeys, "No passkeys yet."),
      data.passkeys.length < data.limit ? form : muted("The passkey limit has been reached."), notice);
  }

  const LOGOUT_DEADLINE_MS = 10000;
  let pendingLogout = Promise.resolve();
  async function logout() {
    const button = $("logout");
    if (button.disabled) return;
    button.disabled = true;
    const { vault, voiceGreeting } = prefs();
    if (voiceGreeting) speak(VAULT.lock.caption);
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), LOGOUT_DEADLINE_MS);
    const revoke = api("api/v1/session", { method: "DELETE", signal: controller.signal })
      .finally(() => clearTimeout(deadline));
    let settled;
    pendingLogout = new Promise((resolve) => { settled = resolve; });
    try {
      const result = await (vault ? playVault("lock", revoke) : revoke);
      if (result.ok || result.status === 401) {
        history.replaceState(null, "", location.pathname + location.search);
        showLogin("You have signed out.");
      }
      else $("view-status").textContent = "Sign-out did not complete. Try again.";
    } finally {
      button.disabled = false;
      settled();
    }
  }

  // Ask Jarvis ------------------------------------------------------------------------------
  const QuestionRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const ASSISTANT_ERRORS = {
    assistant_not_configured: "Ask Jarvis has no AI provider configured on the server.",
    assistant_unavailable: "Jarvis could not reach its AI provider. Try again in a moment.",
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
    question = question.trim().replace(/\s+/g, " ");
    if (!question || asking) return;
    if (question.length > 500) { orb("error", "That question is too long. Keep it under 500 characters."); return; }
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
    if (!QuestionRecognition) { orb("error", `${VOICE_UNSUPPORTED} Type your question instead.`); return; }
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
      orb("error", voiceError(event.error, "Jarvis could not hear you. Try again or type."));
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
      orb("error", `${VOICE_UNSUPPORTED} Type your question instead.`);
    }
  }

  // Data cache: a section whose data is already here renders at once, then refreshes in the background.
  const CACHE_SHOW_MS = 10 * 60 * 1000; // older data is never shown; the section loads afresh
  const CACHE_FRESH_MS = 15 * 1000; // younger data is not re-fetched in the background
  const PREFETCH = ["api/v1/energy", "api/v1/energy/dashboard", "api/v1/instagram/drafts", "api/v1/modules", "api/v1/agents",
    "api/v1/automations", "api/v1/integrations", "api/v1/passkeys", "api/v1/overview"];
  const cache = new Map(); // path -> { json, at }
  const inflight = new Map(); // path -> pending api() result
  const sectionViews = new Map(); // section -> { nodes, title, status } last shown
  let cacheEpoch = 0;
  let renderUses = new Map(); // path -> cache entry served to the current render

  function clearCache() {
    cacheEpoch += 1;
    cache.clear();
    inflight.clear();
    sectionViews.clear();
  }

  function fetchData(path) {
    if (inflight.has(path)) return inflight.get(path);
    const epoch = cacheEpoch;
    const request = api(path).then((result) => {
      if (epoch === cacheEpoch && inflight.get(path) === request) {
        inflight.delete(path);
        if (result.ok) cache.set(path, { json: JSON.stringify(result.data), at: Date.now() });
      }
      return result;
    });
    inflight.set(path, request);
    return request;
  }

  // Drop cached paths; a request already in flight for them is no longer cached when it returns.
  function forget(...paths) {
    for (const path of paths) { cache.delete(path); inflight.delete(path); }
  }

  // Cache data fetched outside load(); returns whether it differs from what was cached.
  function remember(path, data) {
    const json = JSON.stringify(data);
    const changed = cache.get(path)?.json !== json;
    inflight.delete(path);
    cache.set(path, { json, at: Date.now() });
    return changed;
  }

  async function load(path) {
    const entry = cache.get(path);
    if (entry && Date.now() - entry.at < CACHE_SHOW_MS) {
      renderUses.set(path, entry);
      return JSON.parse(entry.json);
    }
    const epoch = cacheEpoch;
    const result = await fetchData(path);
    if (epoch !== cacheEpoch) return null;
    if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return null; }
    if (!result.ok) {
      $("view-status").textContent = `Could not load data (${(result.data && result.data.error) || result.status}).`;
      return null;
    }
    return result.data;
  }

  async function revalidate(current, uses) {
    const stale = [...uses].filter(([, entry]) => Date.now() - entry.at >= CACHE_FRESH_MS);
    if (stale.length === 0) return;
    const epoch = cacheEpoch;
    const results = await Promise.all(stale.map(([path]) => fetchData(path)));
    if (epoch !== cacheEpoch || !session) return;
    if (results.some((result) => result.status === 401)) { showLogin("Your session has ended. Sign in again."); return; }
    if (current !== renderCount) return;
    const failed = results.find((result) => !result.ok);
    let notice = "";
    if (failed) {
      const oldest = Math.min(...stale.map(([, entry]) => entry.at));
      notice = `Showing data from ${when(new Date(oldest).toISOString())}; refreshing failed (${(failed.data && failed.data.error) || failed.status}).`;
      $("view-status").textContent = notice;
    }
    if (stale.some(([path, entry]) => cache.has(path) && cache.get(path).json !== entry.json)) render({ background: true, notice });
  }

  async function prefetch() {
    const epoch = cacheEpoch;
    for (const path of PREFETCH) {
      if (epoch !== cacheEpoch || !session) return;
      const entry = cache.get(path);
      if (entry && Date.now() - entry.at < CACHE_FRESH_MS) continue;
      const result = await fetchData(path);
      if (epoch !== cacheEpoch || !session) return;
      if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
      if (!result.ok) return;
    }
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

  const shortDay = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", weekday: "short", day: "numeric", month: "short" });
  const dayLabel = (iso) => shortDay.format(new Date(`${iso}T12:00:00Z`));
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  function dayBars(days, fuel) {
    const known = days.filter((d) => typeof d.kwh === "number");
    if (known.length === 0) return muted(`No measured ${fuel} days in the last fortnight.`);
    const max = Math.max(...known.map((d) => d.kwh)) || 1;
    const width = 280 / days.length;
    const chart = svg("svg", { viewBox: "0 0 280 100", class: "bars day-bars", role: "img",
      "aria-label": `${fuel === "gas" ? "Gas" : "Electricity"}, last ${days.length} days: ` +
        days.map((d) => `${dayLabel(d.date)} ${typeof d.kwh === "number" ? `${d.kwh.toFixed(1)} kWh${d.complete === false ? " (partial)" : ""}` : "no reading"}`).join("; ") });
    days.forEach((d, i) => {
      const measured = typeof d.kwh === "number";
      const partial = measured && d.complete === false;
      const height = measured ? Math.max(2, (d.kwh / max) * 74) : 2;
      const recent = i >= days.length - 7;
      chart.append(svg("rect", { x: i * width + 2, y: 80 - height, width: width - 4, height, rx: 2,
        class: measured && !partial ? (recent ? "" : "prev") : "missing" },
      svg("title", {}, `${dayLabel(d.date)}: ${measured ? `${d.kwh.toFixed(2)} kWh (measured${partial ? ", some readings missing" : ""})` : "no reading"}` +
        (typeof d.cost === "number" ? `, £${d.cost.toFixed(2)} (estimated)` : ""))));
      if ((days.length - 1 - i) % 2 === 0) {
        chart.append(svg("text", { x: i * width + width / 2, y: 96, "text-anchor": "middle" }, String(Number(d.date.slice(8)))));
      }
    });
    return chart;
  }

  function fuelWidget(fuel, info) {
    const name = fuel === "gas" ? "Gas" : "Electricity";
    if (!info || info.state !== "available") {
      return widget(name, "Last 14 days", { span: 2, className: "fuel" },
        muted(info ? info.reason : "Energy data is not available yet."), el("a", { href: "#energy" }, "Open energy"));
    }
    const week = info.week;
    const change = typeof week.change === "number" ? week.change : null;
    const delta = change === null ? null : el("span", { class: `delta ${change > 0 ? "up" : "down"}` },
      `${change > 0 ? "▲" : "▼"} ${Math.abs(change * 100).toFixed(0)}% vs previous 7 days`);
    const peak = info.peak_hour ? `Usually peaks ${String(info.peak_hour.hour).padStart(2, "0")}:00–${String((info.peak_hour.hour + 1) % 24).padStart(2, "0")}:00` : null;
    return widget(name, `7 days to ${dayLabel(info.through)}`, { span: 2, className: "fuel" },
      el("div", { class: "fuel-head" },
        el("p", { class: "big-number" }, typeof week.kwh === "number" ? week.kwh.toFixed(1) : "–", el("span", { class: "muted" }, "kWh")),
        el("p", { class: "fuel-meta" }, typeof week.cost === "number" ? el("span", {}, `£${week.cost.toFixed(2)} `, basis("estimated")) : null, delta)),
      dayBars(info.days, fuel),
      el("p", { class: "fuel-foot muted" }, [peak, "Bright bars: last 7 days"].filter(Boolean).join(" · ")));
  }

  function insightItem(item) {
    return el("li", { class: `insight insight-${item.severity}` },
      el("strong", {}, item.title),
      el("p", {}, item.detail),
      el("p", { class: "insight-do" }, el("span", { class: "tag" }, "Suggestion"), " ", item.suggestion, " ",
        el("a", { href: item.href }, "Open energy")));
  }

  const HEALTH = { nominal: "Nominal", degraded: "Degraded", fault: "Fault", standby: "Standby" };

  function svg(tag, attrs, ...children) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function reactor(s) {
    const ring = svg("svg", { viewBox: "0 0 120 120", "aria-hidden": "true", focusable: "false" });
    ring.append(
      svg("circle", { class: "r-outer", cx: 60, cy: 60, r: 56 }),
      svg("circle", { class: "r-ticks", cx: 60, cy: 60, r: 50 }),
      svg("circle", { class: "r-dash", cx: 60, cy: 60, r: 42 }),
      svg("circle", { class: "r-inner", cx: 60, cy: 60, r: 30 }),
      svg("circle", { class: "r-core", cx: 60, cy: 60, r: 18 }),
      svg("circle", { class: "r-heart", cx: 60, cy: 60, r: 8 }));
    const ratio = s.automations_monitored ? `${s.automations_healthy} of ${s.automations_monitored} automations healthy` : "No automation has run yet";
    const open = [s.issues ? plural(s.issues, "issue") : null, s.actions ? `${s.actions} to act on` : null].filter(Boolean);
    let bar = null;
    if (s.automations_monitored) {
      bar = el("div", { class: "hud-bar", "aria-hidden": "true" });
      bar.style.setProperty("--v", String(s.automations_healthy / s.automations_monitored));
    }
    return widget("System status", ratio, { className: `reactor-widget health-${s.state}` },
      el("div", { class: "reactor", role: "img", "aria-label": `System status: ${HEALTH[s.state]}. ${ratio}.` },
        ring, el("p", { class: "reactor-state", "aria-hidden": "true" }, HEALTH[s.state]), bar),
      el("p", { class: "reactor-detail" }, open.length ? open.join(" · ") : "Nothing needs you right now."),
      el("a", { href: "#automations" }, "Automations"));
  }

  function alertItem(a) {
    const label = { action: "ACT NOW", critical: "FAILED", warning: "CHECK" }[a.severity] || a.severity.toUpperCase();
    return el("li", { class: `alert alert-${a.severity}` },
      el("strong", {}, label), " ", a.fact,
      a.href ? [" ", el("a", { href: a.href }, "Open")] : null);
  }

  function instagramWidget(ig) {
    const latest = ig.latest_attempt;
    const rows = [["Made today", typeof ig.drafts_today === "number" ? String(ig.drafts_today) : null],
      ["Next post slot", ig.next_slot ? when(ig.next_slot) : null],
      ["Last drafting run", latest ? `${when(latest.started_at)} · ${latest.outcome}` : null]].filter(([, v]) => v);
    return widget("Instagram", "Daily drafts", {},
      el("p", { class: "big-number" }, String(ig.pending_drafts), el("span", { class: "muted" }, ` draft${ig.pending_drafts === 1 ? "" : "s"} to review`)),
      ig.status === "operational" ? null : muted(ig.summary),
      rows.length ? keyValues(rows) : null,
      el("a", { href: "#instagram" }, ig.pending_drafts ? "Review drafts" : "Open Instagram"));
  }

  async function myDay(view) {
    const data = await load("api/v1/overview");
    if (!data) return;
    const now = new Date(data.generated_at);
    $("view-status").textContent = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", weekday: "long", day: "numeric", month: "long" }).format(now);
    const actions = data.attention.filter((a) => a.severity === "action");
    const issues = data.attention.filter((a) => a.severity === "critical" || a.severity === "warning");
    view.append(
      card("Act on now", actions.length || issues.length ?
        el("ul", { class: "alerts" }, [...actions, ...issues].map(alertItem)) :
        muted("Nothing needs your action right now.")),
      el("div", { class: "widgets" }, instagramWidget(data.instagram),
        widget("Automations", "Today's state", {},
          el("ul", { class: "plain" }, data.automations.map((j) => el("li", {}, status(j.status), ` ${j.name}`))),
          el("a", { href: "#automations" }, "View automations"))));
    await placeholder(view, "my-day");
  }

  async function overview(view) {
    const data = await load("api/v1/overview");
    if (!data) return;
    const now = new Date(data.generated_at);
    const longDate = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", weekday: "long", day: "numeric", month: "long" }).format(now);
    $("view-title").textContent = greeting(now);
    $("view-status").textContent = `${longDate} · Europe/London`;
    const fuels = data.energy.fuels || {};
    const unavailable = { state: data.energy.state, reason: data.energy.state === "refresh_required"
      ? "The energy database needs a refresh before readings can be shown." : "Energy data is not available yet." };
    view.append(el("div", { class: "widgets" },
      reactor(data.status),
      widget("Needs attention", "From your automations' latest runs", { span: 3 },
        data.attention.length === 0 ? muted("All clear. Every automation ran as expected.") :
          el("ul", { class: "alerts" }, data.attention.slice(0, 6).map(alertItem)),
        data.attention.length > 6 ? el("a", { href: "#automations" }, `View all ${data.attention.length}`) : null),
      fuelWidget("electricity", fuels.electricity || unavailable),
      fuelWidget("gas", fuels.gas || unavailable),
      data.insights.length ? widget("Worth knowing", "Suggestions from your energy data, worked out by fixed rules", { span: 3 },
        el("ul", { class: "insights" }, data.insights.map(insightItem))) : null,
      instagramWidget(data.instagram),
    ));
  }
  // Energy -----------------------------------------------------------------------------------
  // The pure helpers (validateData, viewGroups, anomalyModel, chartLayout, ...) are loaded from
  // static/energy-core.js: the same script that runs the standalone /energy-usage dashboard.
  const ENERGY_RELOAD_MS = 5 * 60 * 1000;
  const COST_REASONS = { missing_unit_rate: "missing unit rates", missing_standing_charge: "missing standing charges", unknown_gas_units: "unverified gas units" };

  const energyMoney = (value) => (value === null ? "Cost unavailable"
    : value > 0 && value < 0.005 ? "Under £0.01 estimated cost" : `£${value.toFixed(2)} estimated cost`);
  const energyTime = (value) => (value && !Number.isNaN(Date.parse(value)) ? londonTime.format(new Date(value)) : "unavailable");
  const newest = (...values) => values.filter((value) => value && !Number.isNaN(Date.parse(value)))
    .reduce((a, b) => (a === null || Date.parse(b) > Date.parse(a) ? b : a), null);
  const emptyRefreshStatus = () => ({ schema_version: 1, last_attempt_at: null, last_success_at: null, result: null,
    latest_reading_at: { electricity: null, gas: null }, request_state: null, request_id: null, error_category: null });

  function energyTooltip(group, period, fuel, hourly, model) {
    const usage = group.kwh === null ? "Usage unavailable" : `${numberLabel(group.kwh)} kWh`;
    const nodes = [el("h3", {}, periodLabel(group.key, period)), el("p", { class: "tooltip-range" }, periodRangeLabel(group.key, period)),
      el("dl", {}, el("div", {}, el("dt", {}, "Energy used"), el("dd", {}, usage)),
        el("div", {}, el("dt", {}, period === "latest-day" ? "Usage cost (standing charge excluded)" : "Cost"), el("dd", {}, energyMoney(group.cost))))];
    if (!completePeriod(group, period)) nodes.push(muted("Partial period or missing dates; no high-point assessment."));
    if (model.high.has(group.key)) {
      const estimate = contributorEstimate(group, period, fuel, hourly, model.baseline);
      const excess = group.kwh - model.baseline;
      const percent = model.baseline > 0 ? ` (${numberLabel(excess / model.baseline * 100)}%)` : "";
      const timing = estimate.peakHour === null ? "No positive hourly profile is available."
        : `Historical profile peak ${String(estimate.peakHour).padStart(2, "0")}:00 Europe/London; not this period's measured timing.`;
      nodes.push(el("section", { class: "high-point" }, el("h4", {}, "Unusually high point"),
        el("p", {}, `${numberLabel(excess)} kWh${percent} above the mean. Cutoff ${numberLabel(model.threshold)} kWh · ${model.samples} complete periods.`),
        el("h4", {}, "Likely contributors · estimates, not measured"),
        el("ul", {}, estimate.contributors.map((text) => el("li", {}, text))),
        muted(`Heuristic: ${fuel}, ${estimate.season}, ${estimate.largeUplift ? "at least 50% uplift or non-positive baseline" : "under 50% uplift"}. `
          + `${timing} No device-level data: these are possibilities to investigate, not appliance attribution.`)));
    }
    return nodes;
  }

  // A port of initializeDashboard() from octopus_energy/dashboard.js, drawn with DOM nodes in the HUD theme.
  const energyChoice = { fuel: "electricity", period: "day" }; // kept when a background refresh rebuilds the view
  let energyInstances = 0;
  let energyRefreshOwner = null; // instance tracking a Fetch latest data request; background rebuilds would lose it
  function energyDashboard(root, published, generation) {
    let data = null, { fuel, period } = energyChoice, busy = false, loadError = false;
    let refreshBusy = false, lastRefreshStatus = null, stopped = false, attached = false;
    // Unique per instance, so render() never swaps this live dashboard for an older, stopped copy.
    const instance = root.dataset.instance = String(++energyInstances);
    const release = () => {
      refreshBusy = false;
      if (energyRefreshOwner === instance) energyRefreshOwner = null;
    };
    let repositionTooltip = () => {};
    const canRefresh = Boolean(session) && ["recommend", "execute"].includes(session.permission);

    const statusLine = el("p", { class: "energy-status", role: "status", "aria-live": "polite", "data-state": "loading" }, "Loading dashboard data…");
    const updated = el("p", { class: "energy-updated" }, "Last updated unavailable*");
    const footnote = el("p", { class: "muted energy-footnote" }, "* Fetch latest data requests a protected background sync; "
      + "meter readings can lag by 1–2 days. This page also reloads every 5 minutes; neither is a live meter feed.");
    const refreshButton = el("button", { type: "button", class: "primary" }, "Fetch latest data");
    const refreshStatus = el("p", { class: "energy-refresh-status", role: "status", "aria-live": "polite" });
    const choice = (kind, value, label, active) => el("button", { type: "button", [`data-${kind}`]: value,
      class: active ? "active" : "", "aria-pressed": String(active) }, label);
    const fuelButtons = [choice("fuel", "electricity", "Electricity", fuel === "electricity"), choice("fuel", "gas", "Gas", fuel === "gas")];
    const periodButtons = [["day", "Daily"], ["week", "Weekly"], ["month", "Monthly"], ["year", "Yearly"], ["latest-day", "Latest day"]]
      .map(([value, label]) => choice("period", value, label, value === period));
    const cards = el("div", { class: "grid energy-metrics" });
    const title = el("h2", {}, "Consumption");
    const range = muted("");
    const chart = svg("svg", { class: "energy-chart", viewBox: "0 0 960 400", role: "group", "aria-label": "Consumption chart",
      "aria-describedby": "energy-chart-help energy-chart-method" });
    const tooltip = el("div", { id: "energy-chart-tooltip", class: "chart-tooltip", role: "tooltip", "aria-live": "polite", "aria-atomic": "true" });
    tooltip.hidden = true;
    const scroller = el("div", { class: "chart-scroll" }, chart);
    const stage = el("div", { class: "chart-stage" }, scroller, tooltip);
    const action = el("p", { class: "energy-action" });

    const setStatus = (state, message) => { statusLine.dataset.state = state; statusLine.textContent = message; };

    root.append(
      el("section", { class: "card energy-head", "aria-label": "Energy data status" },
        el("div", { class: "energy-head-row" }, refreshButton, updated),
        canRefresh ? null
          : muted(`Fetch latest data needs RECOMMEND permission; this session is ${session ? session.permission.toUpperCase() : "signed out"}.`),
        statusLine, refreshStatus, footnote),
      el("section", { class: "card energy-filters", "aria-label": "Chart filters" },
        el("div", { class: "energy-filter" }, el("h3", {}, "Fuel"), el("div", { class: "segmented" }, fuelButtons)),
        el("div", { class: "energy-filter" }, el("h3", {}, "Period"), el("div", { class: "segmented" }, periodButtons))),
      cards,
      el("section", { class: "card energy-chart-card" }, title, range,
        stage,
        el("details", { class: "energy-method" }, el("summary", {}, "How this chart works"),
          el("p", { id: "energy-chart-help", class: "muted energy-small" }, "Hover or tap a point for its recorded period. "
            + "Keyboard: Tab to a point, arrows to move, Home/End for endpoints, Enter/Space to show, Escape to dismiss. "
            + "Ringed points are unusually high; gaps mean usage is unavailable. Scroll the chart horizontally on small screens."),
          el("p", { id: "energy-chart-method", class: "muted energy-small" }, "High-point rule: strictly above mean + 2 × population standard deviation, "
          + "reusing the energy analytics rule. Calculated separately for each fuel and selected period in kWh per period, "
          + "using at least 7 complete past numeric periods from the latest 90 plotted periods (including the candidate). "
          + "Null, missing-date and ongoing periods are excluded. Equal values produce no high points. "
          + "Likely contributors appear only on high points and are estimates, not measured devices: "
          + "fuel, calendar season at the period midpoint (whole years are mixed-season), historical hourly peak where available, "
          + "and uplift of at least 50% versus a smaller uplift. Season is not observed weather; an hourly profile is not that point's timing."),
        muted("Latest day shows the most recent complete past Europe/London day for each fuel, with 23, 24 or 25 actual "
          + "hourly buckets. Repeated autumn hours retain their BST/GMT offsets. Coverage is checked for every supply point observed "
          + "that day; replacement meters are combined. Readings crossing hourly boundaries are split by duration. "
          + "Hourly costs cover usage only and exclude standing charge; missing unit rates remain unavailable."))),
      el("div", { class: "grid energy-guidance" },
        el("section", { class: "card" }, el("h2", {}, "Suggested next action"), action),
        el("section", { class: "card" }, el("h2", {}, "Confirmed Octopus events only"),
          muted("No live event feed is connected. For an invited Power Down session, delay flexible demand such as laundry or EV charging. "
            + "For Power Up or Happy Hour, move flexible demand into the exact window confirmed in your Octopus account. "
            + "Never turn off medical, frost-protection or essential devices. Do not increase usage just to chase a reward."),
          el("a", { href: "https://octopus.energy/saving-sessions/", target: "_blank", rel: "noopener noreferrer" }, "Octopus event guidance"))),
    );

    function draw(groups) {
      const focusedKey = chart.contains(document.activeElement) ? document.activeElement.dataset.periodKey : null;
      tooltip.hidden = true;
      repositionTooltip = () => {};
      chart.onpointermove = chart.onclick = chart.onkeydown = null;
      stage.onpointerleave = null;
      scroller.onscroll = null;
      chart.dataset.fuel = fuel;
      if (!groups.some((row) => row.kwh !== null)) {
        chart.replaceChildren();
        chart.setAttribute("aria-label", `No numeric ${fuel} usage available to plot.`);
        return;
      }
      const layout = chartLayout(groups), model = anomalyModel(groups, period);
      const { left, right, top, bottom, points, ticks } = layout;
      const xTitle = { day: "Date", week: "Week commencing", month: "Month", year: "Year", "latest-day": "Time" }[period] + " (Europe/London)";
      let previousKnown = false;
      const path = points.map((point) => {
        if (point.y === null) { previousKnown = false; return ""; }
        const segment = `${previousKnown ? "L" : "M"}${point.x} ${point.y}`;
        previousKnown = true;
        return segment;
      }).join(" ");
      const tickIndices = [...new Set(Array.from({ length: Math.min(6, groups.length) }, (_, index) =>
        Math.round(index * (groups.length - 1) / Math.max(Math.min(6, groups.length) - 1, 1))))];
      const elements = points.filter((point) => point.y !== null).map((point, index) => {
        const group = point.group, high = model.high.has(group.key);
        const label = `${periodLabel(group.key, period)}, ${numberLabel(group.kwh)} kWh, ${energyMoney(group.cost)}${high ? ", unusually high" : ""}`;
        return svg("g", { class: "chart-point", "data-point-index": point.index, "data-period-key": group.key, role: "button",
          tabindex: index === 0 ? 0 : -1, "aria-label": label, "aria-describedby": "energy-chart-tooltip" },
        svg("circle", { class: "point-hit", cx: point.x, cy: point.y, r: 16 }),
        high ? svg("circle", { class: "point-high", cx: point.x, cy: point.y, r: 8 }) : null,
        svg("circle", { class: "point-dot", cx: point.x, cy: point.y, r: 3.5 }));
      });
      const vertical = svg("line", { class: "chart-crosshair", y1: top, y2: bottom });
      const horizontal = svg("line", { class: "chart-crosshair", x1: left, x2: right });
      const marker = svg("circle", { class: "chart-marker", r: 7 });
      const crosshair = svg("g", { class: "chart-crosshair-group", visibility: "hidden", "aria-hidden": "true" }, vertical, horizontal, marker);
      chart.replaceChildren(
        svg("g", { "aria-hidden": "true" },
          ticks.map((tick) => [svg("line", { class: "chart-grid", x1: left, x2: right, y1: tick.y, y2: tick.y }),
            svg("text", { class: "chart-tick", x: left - 14, y: tick.y + 5, "text-anchor": "end" }, numberLabel(tick.value))]),
          svg("path", { class: "chart-axis", d: `M${left} ${top}V${bottom}H${right}` }),
          tickIndices.map((index) => [svg("line", { class: "chart-axis", x1: points[index].x, x2: points[index].x, y1: bottom, y2: bottom + 6 }),
            svg("text", { class: "chart-tick", x: points[index].x, y: bottom + 26,
              "text-anchor": groups.length > 1 && index === groups.length - 1 ? "end" : "middle" }, periodLabel(groups[index].key, period, true))]),
          svg("text", { class: "chart-axis-title", x: (left + right) / 2, y: 382, "text-anchor": "middle" }, xTitle),
          svg("text", { class: "chart-axis-title", transform: "translate(22 170) rotate(-90)", "text-anchor": "middle" }, "Energy used (kWh)"),
          svg("path", { class: "chart-series", d: path })),
        ...elements, crosshair);
      chart.setAttribute("aria-label", `${fuel} ${period} consumption, ${groups[0].key} to ${groups.at(-1).key}. ${xTitle}; Energy used (kWh).`);
      let selected = null;
      function positionTooltip(point) {
        const bounds = chart.getBoundingClientRect(), area = stage.getBoundingClientRect();
        const viewport = window.visualViewport;
        const viewportTop = viewport?.offsetTop ?? 0, viewportLeft = viewport?.offsetLeft ?? 0;
        const viewportHeight = viewport?.height ?? window.innerHeight;
        const viewportWidth = viewport?.width ?? window.innerWidth;
        const leftEdge = Math.max(area.left, viewportLeft) + 8;
        const rightEdge = Math.min(area.left + area.width, viewportLeft + viewportWidth) - 8;
        tooltip.style.maxWidth = `${Math.max(0, rightEdge - leftEdge)}px`;
        tooltip.style.maxHeight = `${Math.max(0, Math.min(440, viewportHeight * 0.7, viewportHeight - 16))}px`;
        const box = tooltip.getBoundingClientRect();
        const x = bounds.left + point.x / 960 * bounds.width;
        const y = bounds.top + point.y / 400 * bounds.height;
        const above = y - box.height - 16;
        const preferredTop = above >= viewportTop + 8 ? above : y + 16;
        const clientTop = Math.max(viewportTop + 8, Math.min(preferredTop, viewportTop + viewportHeight - box.height - 8));
        // Rects are viewport-relative already; subtract the stage rect, not document scroll.
        tooltip.style.left = `${Math.max(leftEdge, Math.min(x - box.width / 2, rightEdge - box.width)) - area.left}px`;
        tooltip.style.top = `${clientTop - area.top}px`;
      }
      function hide() {
        tooltip.hidden = true;
        crosshair.setAttribute("visibility", "hidden");
      }
      function show(index) {
        const point = points[index];
        if (!point || point.y === null) { hide(); return; }
        if (selected === index && !tooltip.hidden) { positionTooltip(point); return; }
        selected = index;
        elements.forEach((element) => element.setAttribute("tabindex", Number(element.dataset.pointIndex) === index ? "0" : "-1"));
        vertical.setAttribute("x1", point.x); vertical.setAttribute("x2", point.x);
        horizontal.setAttribute("y1", point.y); horizontal.setAttribute("y2", point.y);
        marker.setAttribute("cx", point.x); marker.setAttribute("cy", point.y);
        crosshair.setAttribute("visibility", "visible");
        tooltip.replaceChildren(...energyTooltip(point.group, period, fuel, data.hourly, model));
        tooltip.hidden = false;
        positionTooltip(point);
      }
      function focusPoint(position) {
        const element = elements[Math.max(0, Math.min(position, elements.length - 1))];
        element.focus();
        show(Number(element.dataset.pointIndex));
      }
      elements.forEach((element) => {
        element.onfocus = () => show(Number(element.dataset.pointIndex));
        element.onblur = hide;
      });
      function pointerIndex(event) {
        const bounds = chart.getBoundingClientRect();
        const x = (event.clientX - bounds.left) / bounds.width * 960;
        const edgeTolerance = 8; // SVG units: covers the outer marker/ring, not the chart gutters.
        if (x < left - edgeTolerance || x > right + edgeTolerance) return null;
        const clampedX = Math.max(left, Math.min(right, x));
        return groups.length === 1 ? 0 : Math.round((clampedX - left) / (right - left) * (groups.length - 1));
      }
      chart.onpointermove = (event) => {
        if (event.pointerType === "touch") return;
        const index = pointerIndex(event);
        if (index === null) hide(); else show(index);
      };
      chart.onclick = (event) => {
        const index = pointerIndex(event);
        const position = elements.findIndex((element) => Number(element.dataset.pointIndex) === index);
        if (position >= 0) focusPoint(position); else hide();
      };
      chart.onkeydown = (event) => {
        const position = Math.max(0, elements.indexOf(document.activeElement));
        const moves = { ArrowRight: position + 1, ArrowDown: position + 1, ArrowLeft: position - 1, ArrowUp: position - 1, Home: 0, End: elements.length - 1 };
        if (Object.hasOwn(moves, event.key)) { event.preventDefault(); focusPoint(moves[event.key]); }
        else if (event.key === "Escape") { event.preventDefault(); hide(); }
        else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); focusPoint(position); }
      };
      stage.onpointerleave = () => { if (!chart.contains(document.activeElement)) hide(); };
      repositionTooltip = () => { if (selected !== null && !tooltip.hidden) positionTooltip(points[selected]); };
      scroller.onscroll = repositionTooltip;
      if (focusedKey) {
        const position = elements.findIndex((element) => element.dataset.periodKey === focusedKey);
        focusPoint(position < 0 ? 0 : position);
      }
    }

    function metric(heading, value, note) {
      return el("section", { class: "card energy-metric" }, el("h2", {}, heading), el("div", { class: "big-number" }, value), muted(note));
    }

    function render() {
      if (!data) return;
      const groups = viewGroups(data, fuel, period);
      const latestDay = period === "latest-day" ? data.latest_day?.[fuel] : null;
      const last = latestDay ? { key: latestDay.date, reasons: new Set(),
        kwh: groups.reduce((sum, row) => sum + row.kwh, 0),
        cost: groups.some((row) => row.cost === null) ? null : groups.reduce((sum, row) => sum + row.cost, 0) } : groups.at(-1);
      const change = trendChange(groups, period);
      const hours = data.hourly.filter((row) => row.fuel === fuel);
      const peak = hours.length ? hours.reduce((a, b) => (a.kwh >= b.kwh ? a : b)) : null;
      const peakLabel = peak ? `${String(peak.hour).padStart(2, "0")}:00` : "No profile";
      const showHourlyPeak = fuel === "electricity" || peak !== null;
      const freshness = readingState(data, fuel);
      const reason = last ? [...last.reasons].map((value) => COST_REASONS[value] || "incomplete source data").join(", ") : "";
      if (loadError) setStatus("error", "Reload failed. Showing the last loaded snapshot; it may be out of date. Check access and retry.");
      else if (period === "latest-day" && !last) setStatus("warning", `No complete London day is available for ${fuel}. Use the daily view; source coverage or confirmed gas units may be unavailable.`);
      else if (!last) setStatus("warning", `No recorded ${fuel} data. Run an authenticated source sync and publish the dashboard.`);
      else if (last.kwh === null) setStatus("warning", "Usage unavailable: source units are unverified. Do not treat raw gas volume as kWh.");
      else if (last.cost === null && freshness.state === "ready") setStatus("warning", "Usage is available, but costs are unavailable because source pricing is incomplete.");
      else setStatus(freshness.state, freshness.text);
      updated.textContent = `Last updated ${energyTime(newest(lastRefreshStatus?.last_success_at, data.last_successful_sync_at))}*`;
      footnote.textContent = "* Last successful sync, Europe/London. Fetch latest data requests a protected background sync; "
        + `meter readings can lag by 1–2 days (latest: electricity ${energyTime(data.latest_reading_at?.electricity)}, `
        + `gas ${energyTime(data.latest_reading_at?.gas)}). The page also reloads every 5 minutes; neither is a live meter feed.`;
      cards.replaceChildren(
        metric(`${fuel} ${period === "latest-day" ? "latest available day" : period}${period === "week" ? " (Monday start)" : ""}`,
          last ? last.kwh === null ? "Usage unavailable" : `${last.kwh.toFixed(1)} kWh` : "No data",
          `${last ? energyMoney(last.cost) : "Cost unavailable"}${reason ? ` (${reason})` : ""}${last ? ` · ${last.key}` : ""}${period === "latest-day" ? " · Usage cost excludes standing charge." : ""}`),
        metric("Recorded-period trend", change === null ? "Not comparable" : `${change >= 0 ? "+" : ""}${change.toFixed(1)}%`,
          period === "latest-day" ? "Compares adjacent recorded hours in the complete London day." : "Requires adjacent past periods with all dates present. Missing intervals are not adjusted."),
        metric("Historical peak hour", showHourlyPeak ? peakLabel : "Review heating",
          showHourlyPeak ? "Europe/London, averaged across available readings. Not a cheapest-rate prediction." : "Compare cold-day demand and heating schedules."));
      title.textContent = `${fuel[0].toUpperCase()}${fuel.slice(1)} ${period} recorded consumption`;
      range.textContent = last ? `${groups[0].key} to ${last.key} · Up to 90 recorded periods. Latest period may be partial.` : "No data for this fuel.";
      if (period === "latest-day") {
        title.textContent = `${fuel[0].toUpperCase()}${fuel.slice(1)} latest available day`;
        const dateLabel = latestDay ? new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", day: "numeric", month: "short", year: "numeric" }).format(new Date(`${latestDay.date}T00:00:00Z`)) : "";
        range.textContent = latestDay ? `Latest available day: ${dateLabel} · ${groups.length} hourly periods · Usage cost excludes standing charge. Full recorded coverage for supply points observed that day.` : "No complete London day available for this fuel.";
      }
      action.textContent = loadError || !last || last.kwh === null || last.cost === null || freshness.state !== "ready"
        ? "Refresh and check meter data and tariff rates before making decisions. Confirm any event invitation in your Octopus account."
        : fuel === "electricity"
          ? "For reward events, follow only your Octopus-confirmed invitation. Outside events, check your tariff before moving flexible loads; your peak-demand hour is not necessarily your highest-cost hour."
          : "If gas demand rises, check heating times, thermostat settings, hot-water demand and draughts. Keep essential heating and frost protection on.";
      draw(groups);
    }

    function accept(snapshot) {
      if (!snapshot || snapshot.state !== "available") throw new Error(snapshot?.state || "unavailable");
      data = validateData(snapshot.dashboard);
      loadError = false;
      render();
    }

    function failed(state) {
      loadError = true;
      if (data) render();
      else if (state === "not_published") setStatus("warning", "No energy snapshot is published yet. The energy runner writes it after its next successful sync; Fetch latest data queues one now.");
      else setStatus("error", "Cannot load dashboard data. Check authentication, network access and published files, then retry.");
    }

    async function load() {
      if (busy || stopped) return false;
      busy = true;
      refreshButton.disabled = true;
      if (!data) setStatus("loading", "Loading dashboard data…");
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const epoch = cacheEpoch;
        const result = await api("api/v1/energy/dashboard", { signal: controller.signal });
        if (epoch !== cacheEpoch) { stop(); return false; } // signed out (or in again) meanwhile
        if (!live()) return false; // navigated away meanwhile: leave the cache to the current view
        if (result.status === 401) { stop(); showLogin("Your session has ended. Sign in again."); return false; }
        if (!result.ok) throw new Error("unavailable");
        accept(result.data);
        if (remember("api/v1/energy/dashboard", result.data)) forget("api/v1/energy");
        return true;
      } catch (error) {
        failed(error.message);
        return false;
      } finally {
        clearTimeout(timeout);
        busy = false;
        refreshButton.disabled = refreshBusy || !canRefresh;
      }
    }

    async function refreshJson(path, options) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const epoch = cacheEpoch;
        const result = await api(path, { ...options, signal: controller.signal });
        if (epoch !== cacheEpoch) { stop(); throw new Error("signed_out"); }
        if (!live()) throw new Error("detached");
        if (result.status === 401) { stop(); showLogin("Your session has ended. Sign in again."); throw new Error("signed_out"); }
        return result;
      } finally { clearTimeout(timeout); }
    }

    async function readRefreshStatus() {
      const result = await refreshJson("api/v1/energy/refresh");
      if (!result.ok || !result.data) throw new Error("Status unavailable");
      return result.data.state === "missing" ? emptyRefreshStatus() : validateRefreshStatus(result.data.status);
    }

    async function requestRefresh() {
      if (refreshBusy || busy || !canRefresh) return;
      refreshBusy = true;
      energyRefreshOwner = instance;
      refreshButton.disabled = true;
      const message = (state, text) => { refreshStatus.dataset.state = state; refreshStatus.textContent = text; };
      const finish = (state, text) => {
        release();
        refreshButton.disabled = busy || !canRefresh;
        message(state, text);
      };
      let beforeReadings = data ? { ...data.latest_reading_at } : null;
      const finishTerminal = (status, previous = false) => {
        const prefix = previous ? "Previous attempt: " : "";
        const queued = status.request_state === "pending"
          ? status.result === "success" ? " Another update is queued." : " A retry is queued." : "";
        if (status.result === "failed" || status.result === "partial") {
          const neutral = status.error_category === "superseded_release";
          finish(neutral ? "requested" : "error", prefix
            + (status.result === "partial" ? "Only part of the source was available. " : "")
            + refreshFailureMessage(status.error_category) + (neutral ? "" : " Old data is kept.") + queued);
        } else {
          const newer = ["electricity", "gas"].some((kind) => Date.parse(data?.latest_reading_at?.[kind]) > Date.parse(beforeReadings[kind] || "1970-01-01T00:00:00Z"));
          const text = status.result === "no_data" || !newer
            ? "No newer readings from Octopus yet. A 1–2 day meter-data lag is normal. The latest published snapshot is kept."
            : "Updated. New latest-reading timestamps and the last successful sync are shown in Data as of.";
          finish("ready", prefix + text + queued);
        }
      };
      message("requested", "Requested: preparing a background fetch.");
      try {
        const baseline = await readRefreshStatus();
        beforeReadings ??= baseline.latest_reading_at;
        const reply = await refreshJson("api/v1/energy/refresh", { method: "POST" });
        const body = reply.data;
        if (reply.status === 403) {
          finish("error", "This session cannot request a refresh (RECOMMEND permission is required). The existing snapshot is kept."); return;
        }
        if (reply.status === 429 && body?.state === "rate_limited") {
          const retry = Number.isFinite(body.retry_after) ? Math.max(1, Math.min(300, Math.ceil(body.retry_after))) : 300;
          finish("warning", `Rate limited. Try again in ${retry} seconds. The existing snapshot is kept.`); return;
        }
        if (!reply.ok || !["pending", "already_running", "retry_queued"].includes(body?.state)) throw new Error("Request unavailable");
        forget("api/v1/energy", "api/v1/energy/dashboard");
        const timestamp = (value) => value === null || (validInstant(value) && value.endsWith("Z"));
        if (!Object.hasOwn(body, "request_id") || !Object.hasOwn(body, "last_attempt_at")
            || !(body.request_id === null || validRequestId(body.request_id))
            || !timestamp(body.last_attempt_at)) throw new Error("Invalid request acknowledgement");
        const requestId = body.request_id;
        const acknowledgedAttempt = body.last_attempt_at;
        const retryAttempt = body.state === "retry_queued" ? body.last_attempt_at : null;
        if (body.state !== "pending" && acknowledgedAttempt === null) throw new Error("Missing acknowledged attempt");
        const joining = body.state === "already_running";
        const untracked = () => finish("warning", "An earlier request is queued or delayed, but its outcome cannot be tracked here. Check Data as of later; the existing snapshot is kept.");
        if (body.state === "pending" && requestId === null) { untracked(); return; }
        const deadline = Date.now() + 10 * 60 * 1000;
        message(joining ? "running" : "requested", joining ? "Already running: fetching from Octopus." : "Requested. Waiting for the background runner.");
        let observedSuccess = baseline.last_success_at;
        const poll = async () => {
          if (stopped || !live()) { release(); return; }
          if (Date.now() >= deadline) {
            finish("warning", "Unable to confirm this request’s outcome. It may still be queued or delayed. The existing snapshot is kept; check Data as of or try again later."); return;
          }
          try {
            const status = await readRefreshStatus();
            lastRefreshStatus = status;
            const successChanged = status.last_success_at !== null
              && (observedSuccess === null || compareUtcTimestamps(status.last_success_at, observedSuccess) > 0);
            if (successChanged) {
              if (busy) { setTimeout(poll, 2000); return; }
              if (!await load()) { finish("error", "The update finished, but the published snapshot could not be reloaded. Old data is kept."); return; }
              observedSuccess = status.last_success_at;
            }
            const attemptOrder = status.last_attempt_at !== null && acknowledgedAttempt !== null
              ? compareUtcTimestamps(status.last_attempt_at, acknowledgedAttempt) : null;
            const tracked = requestId !== null && status.request_id === requestId && status.last_attempt_at !== null
              && (body.state === "pending" || (attemptOrder !== null && attemptOrder >= 0));
            const activeJoin = joining && requestId === null && attemptOrder === 0;
            const legacyRetry = retryAttempt !== null && requestId === null && status.request_id === null
              && attemptOrder === 0 && status.request_state === "pending"
              && ["no_data", "partial", "failed"].includes(status.result);
            const matches = tracked || activeJoin || legacyRetry;
            if ((status.request_state === "running" || status.result === "running") && matches) {
              message("running", "Fetching from Octopus…");
            } else {
              const previousRetry = body.state === "retry_queued" && matches && attemptOrder === 0
                && status.request_state === "pending" && ["no_data", "partial", "failed"].includes(status.result);
              if (["success", "no_data", "partial", "failed"].includes(status.result) && matches) {
                finishTerminal(status, previousRetry); return;
              }
              if (body.state === "retry_queued" && requestId === null) { untracked(); return; }
              message("requested", status.result === "running" || status.request_state === "running"
                ? "Requested. Another update is running; this request is still queued."
                : "Requested. Waiting for the background runner to handle this request.");
            }
            setTimeout(poll, 2000);
          } catch { if (!stopped) finish("error", "Refresh status is unavailable. The existing snapshot is kept; check again later."); }
        };
        await poll();
      } catch { if (!stopped) finish("error", "The refresh could not be requested. The existing snapshot is kept; check access and try again."); }
    }

    for (const [kind, buttons] of [["fuel", fuelButtons], ["period", periodButtons]]) {
      for (const button of buttons) {
        button.addEventListener("click", () => {
          if (kind === "fuel") fuel = energyChoice.fuel = button.dataset.fuel;
          else period = energyChoice.period = button.dataset.period;
          for (const other of buttons) {
            other.classList.toggle("active", other === button);
            other.setAttribute("aria-pressed", String(other === button));
          }
          render();
        });
      }
    }
    refreshButton.disabled = !canRefresh;
    refreshButton.addEventListener("click", requestRefresh);

    // Passive reload while visible, as on /energy-usage. Listeners detach once the view is navigated away.
    const onVisible = () => { if (live() && document.visibilityState === "visible") load(); };
    const onViewport = () => { if (live()) repositionTooltip(); };
    const timer = setInterval(onVisible, ENERGY_RELOAD_MS);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => repositionTooltip());
    // Stops once it leaves the page, or once a newer render starts before it was ever shown.
    function live() {
      if (root.isConnected) { attached = true; return true; }
      if (attached || generation !== renderCount) stop();
      return false;
    }
    function stop() {
      if (stopped) return;
      stopped = true;
      if (refreshBusy) release();
      clearInterval(timer);
      observer?.disconnect();
      document.removeEventListener("visibilitychange", onVisible);
      for (const event of ["resize", "scroll"]) {
        window.removeEventListener(event, onViewport);
        window.visualViewport?.removeEventListener(event, onViewport);
      }
    }
    document.addEventListener("visibilitychange", onVisible);
    for (const event of ["resize", "scroll"]) {
      window.addEventListener(event, onViewport, { passive: true });
      window.visualViewport?.addEventListener(event, onViewport, { passive: true });
    }
    observer?.observe(tooltip);
    try { accept(published); } catch (error) { failed(error.message); }
  }

  // Source records: one card per fuel, never a combined table.
  const FUELS = [["electricity", "Electricity"], ["gas", "Gas"]];
  const fuelDay = (iso, options) => (typeof iso === "string" && /^\d{4}-\d{2}-\d{2}$/.test(iso)
    ? new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", day: "numeric", month: "short", year: "numeric", ...options })
      .format(new Date(`${iso}T00:00:00Z`)) : "Unavailable");
  const fuelKwh = (v) => (typeof v === "number" ? `${v.toLocaleString("en-GB", { maximumFractionDigits: 1 })} kWh` : "Unavailable");
  const fuelMoney = (v) => (typeof v === "number" ? `£${v.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "Unavailable");
  const sumOf = (rows, key) => (rows.length && rows.every((row) => typeof row[key] === "number")
    ? rows.reduce((total, row) => total + row[key], 0) : null);
  const dayNumber = (iso) => Date.parse(`${iso}T00:00:00Z`) / 86400000;
  const londonDate = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", dateStyle: "medium" });
  const fuelSince = (value) => (Number.isNaN(Date.parse(value)) ? String(value) : londonDate.format(new Date(value)));
  const londonToday = () => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/London" }).format(new Date());

  function fuelStat(label, value, ...notes) {
    return el("div", { class: "fuel-stat" }, el("dt", {}, label), el("dd", {}, el("strong", {}, value),
      notes.filter(Boolean).map((note) => el("span", {}, note))));
  }

  function energyFuelCard(data, fuel, name) {
    const mine = (rows) => (rows || []).filter((row) => row.fuel === fuel);
    const total = mine(data.totals)[0] || null;
    const span = mine(data.coverage)[0] || null;
    const daily = mine(data.daily).slice().sort((a, b) => b.date.localeCompare(a.date)).slice(0, 14);
    const monthly = mine(data.monthly).slice(0, 12);
    const unusual = mine(data.anomalies);
    const unusualDays = new Set(unusual.map((row) => row.date));
    const tariff = mine(data.tariffs).sort((a, b) => (a.valid_to === null) - (b.valid_to === null)
      || String(a.valid_from || "").localeCompare(String(b.valid_from || ""))).at(-1) || null;

    let period = "No stored days";
    if (span) {
      const calendar = dayNumber(span.last_date) - dayNumber(span.first_date) + 1;
      period = `${fuelDay(span.first_date)} – ${fuelDay(span.last_date)} · ${calendar} day${calendar === 1 ? "" : "s"}`
        + (span.days < calendar ? ` (${span.days} with data)` : "");
    }
    const recentKwh = sumOf(daily, "consumption_kwh"), recentCost = sumOf(daily, "total_cost_gbp");
    const recentRange = daily.length ? `${fuelDay(daily.at(-1).date, { year: undefined })} – ${fuelDay(daily[0].date, { year: undefined })}` : "No recent days";
    let trend = null;
    const latestWeek = daily.slice(0, 7), priorWeek = daily.slice(7, 14);
    const consecutive = daily.length === 14 && daily.every((row, index) => index === 0 || dayNumber(daily[index - 1].date) - dayNumber(row.date) === 1);
    const latestKwh = sumOf(latestWeek, "consumption_kwh"), priorKwh = sumOf(priorWeek, "consumption_kwh");
    if (consecutive && latestKwh !== null && priorKwh) trend = (latestKwh - priorKwh) / priorKwh * 100;

    const peak = daily.filter((row) => typeof row.consumption_kwh === "number")
      .reduce((best, row) => (best === null || row.consumption_kwh > best.consumption_kwh ? row : best), null);
    const today = londonToday();
    const monthLabel = (row) => {
      const label = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", month: "short", year: "numeric" }).format(new Date(`${row.month}-01T00:00:00Z`));
      if (span && row.month === span.last_date.slice(0, 7) && row.month === today.slice(0, 7)) return `${label} (to ${fuelDay(span.last_date, { year: undefined })})`;
      if (span && row.month === span.first_date.slice(0, 7) && !span.first_date.endsWith("-01")) return `${label} (from ${fuelDay(span.first_date, { year: undefined })})`;
      return label;
    };
    const dayCell = (row) => (unusualDays.has(row.date)
      ? el("span", {}, fuelDay(row.date, { weekday: "short", year: undefined }), " ", el("span", { class: "fuel-flag" }, "Unusual"))
      : fuelDay(row.date, { weekday: "short", year: undefined }));
    const dailyTable = table([["Day", dayCell], ["Usage", (r) => fuelKwh(r.consumption_kwh)], ["Est. cost", (r) => fuelMoney(r.total_cost_gbp)]],
      daily, "No daily data in the last 14 days.");
    dailyTable.querySelectorAll("tbody tr").forEach((row, index) => { if (unusualDays.has(daily[index].date)) row.classList.add("fuel-unusual"); });

    return el("section", { class: "card fuel-card", "data-fuel": fuel, "aria-label": `${name} records` },
      el("header", { class: "fuel-head" }, el("h2", {}, name),
        el("p", { class: "fuel-tariff" }, tariff
          ? [el("span", { class: "muted" }, "Tariff "), el("strong", {}, tariff.tariff_code || "Unavailable"),
            el("span", { class: "muted" }, tariff.valid_from ? ` · since ${fuelSince(tariff.valid_from)}` : "")]
          : el("span", { class: "muted" }, "No tariff stored"))),
      el("dl", { class: "fuel-stats" },
        fuelStat("Total, all stored days", fuelKwh(total?.consumption_kwh ?? null), fuelMoney(total?.total_cost_gbp ?? null), period),
        fuelStat("Last 14 days", fuelKwh(recentKwh), recentCost === null ? "Cost unavailable"
          : `${fuelMoney(recentCost)} · ${fuelMoney(recentCost / daily.length)}/day`, recentRange),
        fuelStat("Last 7 days vs previous 7", trend === null ? "Not comparable" : `${trend >= 0 ? "+" : ""}${trend.toFixed(1)}%`,
          trend === null ? "Needs 14 consecutive days of usage" : `${fuelKwh(latestKwh)} vs ${fuelKwh(priorKwh)}`),
        fuelStat("Highest day, last 14 days", peak ? fuelKwh(peak.consumption_kwh) : "Unavailable",
          peak ? `${fuelDay(peak.date, { weekday: "short", year: undefined })} · ${fuelMoney(peak.total_cost_gbp)}` : "No numeric usage")),
      el("div", { class: "fuel-tables" },
        el("div", {}, el("h3", {}, "Daily"), dailyTable),
        el("div", {}, el("h3", {}, "Monthly estimates"), table([["Month", monthLabel], ["Usage", (r) => fuelKwh(r.consumption_kwh)],
          ["Est. cost", (r) => fuelMoney(r.total_cost_gbp)]], monthly, "No monthly data."))),
      unusual.length ? el("div", { class: "fuel-unusual-list" }, el("h3", {}, "Unusual days"),
        el("ul", {}, unusual.map((row) => el("li", {}, el("strong", {}, fuelDay(row.date, { weekday: "short" })),
          ` · ${fuelKwh(row.consumption_kwh)}, above the ${fuelKwh(row.threshold_kwh)} threshold`)))) : null);
  }

  async function energy(view, options) {
    const generation = renderCount; // the render this view belongs to, read before awaiting
    const [data, published] = await Promise.all([load("api/v1/energy"), load("api/v1/energy/dashboard")]);
    if ((!data && !published) || generation !== renderCount || (options?.background && energyRefreshOwner !== null)) return;
    const dashboard = el("div", { class: "energy-dashboard" });
    if (published) energyDashboard(dashboard, published, generation);
    else dashboard.append(card("Energy dashboard", el("p", { class: "energy-status", "data-state": "error" },
      "The published dashboard snapshot could not be loaded. Electricity and gas records below are still shown; reload to retry.")));
    const legend = el("p", { class: "energy-legend", "aria-label": "How to read these numbers" },
      el("span", {}, basis("measured"), " Usage from smart-meter readings"),
      el("span", {}, basis("estimated"), " Costs from stored tariff rates, not bills"),
      el("span", {}, basis("inferred"), " Unusual: above mean + 2 standard deviations"));
    view.append(dashboard, el("div", { class: "energy-records-head" }, el("h2", { class: "energy-section-title" }, "Usage, costs and tariffs"), legend));
    if (!data) {
      view.append(el("p", { class: "energy-status", "data-state": "error" },
        "Electricity and gas records could not be loaded. The dashboard above is still shown; reload to retry."));
      return;
    }
    const h = data.health;
    if (h.status !== "operational") {
      view.append(el("p", { class: "energy-status", "data-state": h.status === "failed" ? "error" : "warning" }, status(h.status), " ", h.summary));
    }
    if (data.state === "available") view.append(...FUELS.map(([fuel, name]) => energyFuelCard(data, fuel, name)));
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

  const SIGN_IN_METHODS = { passphrase: "Passphrase", passkey: "Passkey" };

  async function sessionsCard() {
    const epoch = cacheEpoch;
    const result = await api("api/v1/sessions");
    if (epoch !== cacheEpoch) return null;
    if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return null; }
    if (!result.ok) {
      const retry = el("button", { type: "button" }, "Try again");
      retry.addEventListener("click", () => render());
      return card("Signed-in devices", muted(`Signed-in devices could not be loaded (${(result.data && result.data.error) || result.status}).`),
        el("div", { class: "settings-actions" }, retry));
    }
    const { sessions, sign_in_activity: activity } = result.data;
    const notice = el("p", { role: "status", "aria-live": "polite" }, sessionsNotice);
    const revoke = async (body, button) => {
      button.disabled = true;
      sessionsNotice = "";
      notice.textContent = "";
      const signIn = signInCount;
      const done = await api("api/v1/sessions/revoke", { method: "POST", body });
      if (signIn !== signInCount) return;
      if (done.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
      if (!done.ok) sessionsNotice = "That device could not be signed out.";
      clearCache(); auditView.page = 1; render();
    };
    const device = (item) => item.current ? `${item.device} (this device)` : item.device;
    const action = (item) => {
      if (item.current) return "";
      const button = el("button", { type: "button", "aria-label": `Sign out ${item.device}` }, "Sign out");
      button.addEventListener("click", () => revoke({ id: item.id }, button));
      return button;
    };
    const others = sessions.filter((item) => !item.current).length;
    const all = el("button", { type: "button" }, "Sign out all other devices");
    all.addEventListener("click", () => revoke({ others: true }, all));
    const failed = activity.failed_attempts;
    return card("Signed-in devices",
      muted("Every browser signed in to Jarvis right now. Sign out any you don't recognise, then change the passphrase through the deploy if needed."),
      table([["Device", device], ["Last active", (r) => when(r.last_active_at)],
        ["Expires", (r) => `${when(r.expires_at)}${r.remembered ? " (remembered)" : ""}`], ["", action]], sessions, "No active sessions."),
      others ? el("div", { class: "settings-actions" }, all) : muted("No other devices are signed in."),
      el("h3", {}, "Recent sign-ins"),
      keyValues([[`Failed attempts (last ${activity.failed_window_hours} h)`, failed ? `${failed} — check the audit log` : "None"]]),
      table([["When", (r) => when(r.at)], ["Method", (r) => SIGN_IN_METHODS[r.method] || r.method || "Unknown"],
        ["Device", (r) => r.device || "Unknown"], ["From", (r) => r.client || "Unknown"]], activity.recent_sign_ins, "No sign-ins recorded."),
      notice);
  }

  function preferencesCard() {
    const current = prefs();
    const notice = el("p", { class: "muted", role: "status", "aria-live": "polite" });
    const saved = (update) => {
      notice.textContent = savePrefs(update) ? "Saved on this device." : "This browser blocked saving preferences.";
    };
    const select = (id, options, value, onChange) => {
      const node = el("select", { id }, options.map(([v, label]) => {
        const option = el("option", { value: String(v) }, label);
        if (String(v) === String(value)) option.selected = true;
        return option;
      }));
      node.addEventListener("change", () => onChange(node.value));
      return node;
    };
    const toggle = (id, label, value, key) => {
      const box = el("input", { id, type: "checkbox" });
      box.checked = value;
      box.addEventListener("change", () => saved({ [key]: box.checked }));
      return el("label", { class: "check", for: id }, box, label);
    };
    return card("Display preferences",
      muted("These apply to this browser only and are kept on this device, not on the server."),
      el("div", { class: "prefs" },
        el("label", { for: "pref-start" }, "Start page after sign-in"),
        select("pref-start", SECTIONS, current.startPage, (v) => saved({ startPage: v })),
        el("label", { for: "pref-audit-size" }, "Audit log entries per page"),
        select("pref-audit-size", AUDIT_PAGE_SIZES.map((n) => [n, String(n)]), current.auditPageSize, (v) => {
          saved({ auditPageSize: Number(v) });
          auditView.page = 1;
          render();
        }),
        toggle("pref-vault", "Vault animation when signing in and out", current.vault, "vault"),
        toggle("pref-voice", "Spoken “Access granted” greeting", current.voiceGreeting, "voiceGreeting")),
      notice);
  }


  async function auditCard() {
    const body = el("div", {});
    const section = card("Audit log", muted("Operational metadata only. Content and secrets are never recorded."), body);
    const state = auditView;
    const perPage = prefs().auditPageSize;
    let generation = 0;
    const query = (extra) => {
      const params = new URLSearchParams(extra);
      if (state.action) params.set("action", state.action);
      return params.toString();
    };
    async function show() {
      const ticket = ++generation;
      const epoch = cacheEpoch;
      const result = await api(`api/v1/audit?${query({ page: state.page, per_page: perPage })}`);
      if (epoch !== cacheEpoch) return false;
      if (ticket !== generation) return true;
      if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return false; }
      if (!result.ok) { body.replaceChildren(muted(`The audit log could not be loaded (${(result.data && result.data.error) || result.status}).`)); return true; }
      const data = result.data;
      const filter = el("select", { id: "audit-action" }, el("option", { value: "" }, "All actions"),
        data.actions.map((name) => {
          const option = el("option", { value: name }, name.replace(/_/g, " "));
          if (name === state.action) option.selected = true;
          return option;
        }));
      filter.addEventListener("change", () => { state.action = filter.value; state.page = 1; show(); });
      const exports = ["csv", "json"].map((kind) => el("a", { class: "button-link", href: `api/v1/audit/export?${query({ format: kind })}`,
        download: "" }, `Export ${kind.toUpperCase()}`));
      const prev = el("button", { type: "button" }, "Previous");
      const next = el("button", { type: "button" }, "Next");
      prev.disabled = data.page <= 1;
      next.disabled = data.page >= data.pages;
      prev.addEventListener("click", () => { state.page = Math.max(1, data.page - 1); show(); });
      next.addEventListener("click", () => { state.page = data.page + 1; show(); });
      body.replaceChildren(
        el("div", { class: "audit-tools" }, el("label", { for: "audit-action" }, "Action"), filter, ...exports),
        table([["When", (r) => when(r.at)], ["Actor", (r) => r.actor], ["Action", (r) => r.action], ["Outcome", (r) => r.outcome]],
          data.entries, "No audit entries."),
        el("div", { class: "pager" }, prev,
          el("span", { "aria-live": "polite" }, `Page ${data.page} of ${data.pages} · ${data.total} entr${data.total === 1 ? "y" : "ies"}`), next),
        muted(`Exports hold up to 10,000 of the newest entries${state.action ? " for the chosen action" : ""}; each export is itself audited.`));
      return true;
    }
    return (await show()) ? section : null;
  }

  async function settings(view) {
    const [jobs, agentData, instagramData, sessionsSection, passkeys, audit] = await Promise.all([
      load("api/v1/automations"), load("api/v1/agents"), load("api/v1/instagram/drafts"), sessionsCard(), passkeyCard(), auditCard()]);
    if (!session) return;
    const unavailable = "Could not be loaded right now.";
    const canDecide = ["recommend", "execute"].includes(session.permission);
    view.append(
      card("Session", keyValues([["Permission", session.permission.toUpperCase()], ["Session expires", when(session.expires_at)],
        session.remembered ? ["Remembered device", "Yes, until the session expires or you sign out"]
          : ["Idle timeout", `${Math.round(session.idle_timeout_seconds / 60)} minutes`]]),
      el("ul", {}, el("li", {}, "READ: retrieve and analyse data."),
        el("li", {}, "RECOMMEND: suggest changes and prepare drafts."),
        el("li", {}, "EXECUTE: perform explicitly approved operations. No execute operations exist yet."))),
      sessionsSection,
      passkeys,
      preferencesCard(),
      card("Agents and automations",
        muted("These settings are read-only here. Schedules and agent definitions change through the repository; enable, disable and run-now controls arrive in Phase 5."),
        jobs ? table([["Automation", (r) => r.name], ["Status", (r) => status(r.status)], ["Trigger", (r) => r.trigger]], jobs.automations, "No automations.")
          : muted(`Automations: ${unavailable}`),
        agentData ? table([["Agent", (r) => r.name], ["Guardrail", (r) => r.guardrail], ["Definition", (r) => el("code", {}, r.definition)]], agentData.agents, "No agents.")
          : muted(`Agents: ${unavailable}`),
        keyValues([["Instagram draft approvals", canDecide ? "You can approve or reject drafts on the Instagram page." : "Read-only: your permission cannot approve drafts."],
          ["Instagram automatic posting", !instagramData ? unavailable : instagramData.state !== "available" ? "Unknown: no Instagram database on this server"
            : instagramData.auto_publish ? "On: drafts that pass review post at their publish time" : "Off: nothing posts automatically"],
          ["Posting kill switch", "Repository variable INSTAGRAM_AUTO_PUBLISH (0 stops posting); applies on the next deploy"]])),
      audit);
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
      const activity = agent.activity;
      view.append(card(agent.name, el("p", {}, status(agent.status), " ", agent.summary),
        activity ? [el("h3", {}, "Live activity"), el("p", {}, status(activity.status), " ", activity.summary),
          keyValues([["Awaiting review", String(activity.pending_drafts)],
            ["Last drafting run", activity.latest_run ? `${when(activity.latest_run.started_at)} · ${activity.latest_run.outcome}` : "None recorded"]]),
          el("a", { href: activity.href }, activity.pending_drafts ? "Review drafts" : "Open Instagram")] : null,
        keyValues([["Runs in", agent.runs_in], ["Definition", agent.definition], ["Guardrail", agent.guardrail]]),
        agent.commands.length ? [el("h3", {}, "Commands"),
          table([["Command", (r) => el("code", {}, r.command)], ["What it does", (r) => r.purpose]], agent.commands)] : null));
    }
  }

  const TOKEN_STATUS = {
    not_configured: "Not configured", unknown: "Expiry unknown", valid: "Valid", expiring: "Expiring soon",
    expired: "Expired", refresh_failed: "Refresh failed", externally_managed: "Externally managed",
    unsupported: "Automatic renewal unsupported",
  };

  function tokenCard(token) {
    const notes = {
      not_configured: "No Instagram token is configured in the server environment file.",
      unknown: "Expiry stays unknown until the first successful token refresh.",
      valid: "The server refreshes this Instagram Login token before it expires.",
      expiring: "The token expires soon. Check the last refresh result.",
      expired: "The token has expired. Replace it through the private server setup flow.",
      refresh_failed: "The current token is unchanged. The next refresh retry is backed off for 24 hours.",
      externally_managed: "The process environment owns this token, so automatic server-file renewal is off.",
      unsupported: "Only Instagram Login long-lived tokens can be renewed automatically.",
    };
    return card("Access token",
      keyValues([
        ["Status", TOKEN_STATUS[token.state] || token.state],
        ["Expires", when(token.expires_at)],
        ["Last successful refresh", when(token.last_refreshed_at)],
        ["Last refresh attempt", when(token.last_attempt_at)],
      ]),
      muted(token.last_error || notes[token.state] || "Token lifecycle state is unavailable."));
  }

  const DRAFT_STATUS = {
    pending: "Awaiting review", approved: "Approved", rejected: "Rejected", published: "Published", failed: "Failed",
  };

  const DECISION_ERRORS = {
    publishing: "It's being posted to Instagram now, so it can no longer be approved or rejected.",
    hold_changed: "The hold was changed elsewhere. Refresh to see it.",
    already_decided: "It was already approved or rejected. Refresh to see its status.",
  };

  async function decide(draft, decision, buttons, status) {
    for (const button of buttons) button.disabled = true;
    const result = await api("api/v1/instagram/decision", { method: "POST", body: { draft_id: draft.draft_id, decision } });
    if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
    if (!result.ok) {
      const code = (result.data && result.data.error) || result.status;
      status.textContent = `Could not save the decision. ${DECISION_ERRORS[code] || `(${code})`}`;
      if (code !== "publishing" && code !== "already_decided") for (const button of buttons) button.disabled = false;
      else clearCache();
      return;
    }
    clearCache();
    render();
  }

  // A hold change the server never confirmed: resent on every render until it is, so a delayed copy is fenced.
  const unconfirmedHold = (id) => { try { return JSON.parse(sessionStorage.getItem(`ig-hold:${id}`)); } catch { return null; } };
  const rememberHold = (id, saved) => {
    try { sessionStorage.setItem(`ig-hold:${id}`, JSON.stringify(saved)); return true; } catch { return false; }
  };
  const forgetHold = (id, token) => { // only the change this request saved, never a newer one
    try { const saved = unconfirmedHold(id); if (saved && saved.token === token) sessionStorage.removeItem(`ig-hold:${id}`); } catch { /* best effort */ }
  };

  const holdRequests = new Map();
  const holdFailures = new Map(); // survive dashboard refreshes until acknowledged; cleared on sign-out
  function holdFailureCards() {
    return holdFailures.size ? [card("Hold changes needing attention", [...holdFailures].map(([id, notice]) => {
      const acknowledge = el("button", { type: "button" }, "Dismiss notice");
      acknowledge.addEventListener("click", () => {
        holdFailures.delete(id);
        refreshHoldNotices();
      });
      return el("div", {}, el("p", { role: "status" }, `${notice.topic}: ${notice.message}`), acknowledge);
    }))] : [];
  }
  function refreshHoldNotices() {
    const target = $("ig-hold-notices");
    // Refresh only the live notice region, never a dashboard/form or a detached render's DOM.
    if (target) target.replaceChildren(...holdFailureCards());
  }
  const pendingHold = (draft) => {
    const pending = unconfirmedHold(draft.draft_id);
    return pending && pending.body && pending.body.draft_id === draft.draft_id
      && typeof pending.body.hold === "boolean" && typeof pending.token === "string" ? pending : null;
  };

  // Shared by dashboard recovery and visible controls: a lazy panel must never delay a saved hold.
  function sendHold(draft, body, token, notify = () => {}) {
    const key = JSON.stringify([signInCount, draft.draft_id, token]);
    const active = holdRequests.get(key);
    if (active) { active.listeners.add(notify); return active.promise; }
    const request = { listeners: new Set([notify]) };
    holdRequests.set(key, request);
    const definite = (r) => r.ok || [400, 401, 404, 409].includes(r.status);
    // A request that hangs counts as unanswered, so the retries and the unknown-state warning still happen.
    const deadline = () => {
      if (typeof AbortSignal !== "undefined" && AbortSignal.timeout) return AbortSignal.timeout(15000);
      if (typeof AbortController === "undefined") return undefined;
      const controller = new AbortController();
      setTimeout(() => controller.abort(), 15000);
      return controller.signal;
    };
    request.promise = (async () => {
      // Saved before sending, so a reload while this is in flight resends it rather than trusting the old state.
      const saved = rememberHold(draft.draft_id, { token, body });
      const signIn = signInCount;
      let result = await api("api/v1/instagram/hold", { method: "POST", body, signal: deadline() });
      // Resending the same versioned hold is harmless, so resend until the server gives a definite answer.
      for (let tries = 0; !definite(result) && tries < 3 && signIn === signInCount; tries++) {
        for (const update of request.listeners) update("Couldn't confirm the hold change. Trying again…");
        await new Promise((resolve) => setTimeout(resolve, 2000));
        if (signIn !== signInCount) break;
        result = await api("api/v1/instagram/hold", { method: "POST", body, signal: deadline() });
      }
      if (signIn !== signInCount) { if (result.ok) clearCache(); return; } // an older sign-in's reply
      if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
      if (!definite(result)) {
        const message = saved
          ? "The hold state is unknown: the server didn't answer. It will be resent when this page reloads."
          : "The hold state is unknown: the server didn't answer and this browser can't remember the change. Set it again before relying on it.";
        return { unknown: true, message };
      }
      forgetHold(draft.draft_id, token);
      if (!result.ok) {
        const code = (result.data && result.data.error) || result.status;
        const message = `Could not change the hold. ${DECISION_ERRORS[code] || `(${code})`}`;
        // Save before render(): this card may be hidden or replaced by another replay's success.
        holdFailures.set(draft.draft_id, { topic: draft.topic || draft.draft_id, message });
        refreshHoldNotices();
        if (code === "hold_changed") { clearCache(); render(); } // rebuild with the current hold and version
        return { ok: false, message };
      }
      clearCache();
      render();
      return { ok: true, message: "Hold change confirmed." };
    })().finally(() => { holdRequests.delete(key); });
    return request.promise;
  }

  function holdToggle(draft, status, auto) {
    const box = el("input", {
      type: "checkbox", id: `ig-hold-${draft.draft_id}`, "data-hold": String(draft.reel.hold), "data-hold-version": String(draft.reel.hold_version),
    });
    box.checked = draft.reel.hold;
    async function send(body, token = `${Date.now()}-${Math.random()}`) {
      box.disabled = true;
      const result = await sendHold(draft, body, token, (message) => { status.textContent = message; });
      if (!result) return; // signed out while the request was pending
      status.textContent = result.message;
      box.indeterminate = Boolean(result.unknown);
      if (!result.ok && !result.unknown) { box.checked = draft.reel.hold; box.disabled = false; }
    }
    box.addEventListener("change", () => send({ draft_id: draft.draft_id, hold: box.checked, version: draft.reel.hold_version }));
    const pending = pendingHold(draft);
    if (pending) {
      box.indeterminate = true;
      queueMicrotask(() => {
        status.textContent = "Confirming an earlier hold change…";
        send(pending.body, pending.token);
      });
    }
    return el("p", {}, box, " ", el("label", { for: box.id }, auto
      ? "Hold for my approval (otherwise it posts automatically if it scores 70 or more)"
      : "Hold for my approval (automatic posting is off right now, so nothing posts on its own)"));
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
    if (draft.kind === "reel" && (!draft.reel || draft.reel.source !== "photos")) return el("div", { class: "ig-actions" }, copy);
    const downloads = draft.slides.map((slide, i) =>
      el("a", { href: slideUrl(draft, i + 1), download: `instafoodielove-${draft.created_on}-${i + 1}.${slide.format}` }, `Slide ${i + 1}`));
    return el("div", { class: "ig-actions" }, copy, el("span", { class: "ig-downloads" }, "Download: ", downloads));
  }

  function publishNote(draft, auto) {
    if (draft.publishing) return muted("Posting to Instagram now. It can no longer be approved or rejected.");
    if (draft.publish_error) return muted(`Last publish attempt failed: ${draft.publish_error}`);
    if (draft.kind === "reel" && draft.reel && draft.reel.render === "pending") return muted("The reel is being made on the next daily run; it can't post until then.");
    if (draft.kind === "reel" && draft.reel && draft.reel.render === "failed") return muted(`The reel couldn't be made (${draft.reel.render_error || "unknown error"}), so it won't post. Delete it or send the media again.`);
    if (!auto) return null;
    if (draft.will_post && draft.posts_at) return muted(`Posts to Instagram automatically at ${when(draft.posts_at)} unless you reject it before then.`);
    if (draft.kind === "reel" && draft.reel && draft.reel.hold && draft.status === "pending") return muted("Held for your approval: it only posts once you approve it.");
    if (draft.status === "pending") return muted("Scored under 70, so it only posts if you approve it.");
    return null;
  }

  function reelScript(reel) {
    const at = (s) => `${Number(s).toFixed(1)} s`;
    return el("div", { class: "ig-reel" },
      el("p", {}, el("strong", {}, "Reel hook: "), reel.hook),
      reel.on_screen.length ? el("ol", {}, reel.on_screen.map((beat) => el("li", {}, `${at(beat.start)}-${at(beat.end)}: ${beat.text}`))) : null,
      muted(reelMaking(reel, at)));
  }

  function reelMaking(reel, at) {
    const cover = `Cover frame at ${at(reel.cover_seconds)} of ${at(reel.seconds)}.`;
    if (!reel.render) return `On-screen text is a script for your editor; the clip posts as filmed with its own audio. ${cover}`;
    const from = reel.source === "photos" ? "Your photos become a slideshow with slow zooms" : reel.trim ? `Your clip is cut to ${at(reel.trim.in)}-${at(reel.trim.out)}` : "Your clip is used";
    const sound = !reel.music ? (reel.source === "photos" ? "no music" : "its own audio")
      : `music "${reel.music.title}" (${reel.music.licence}) ${reel.music.mix === "replace" || reel.source === "photos" ? "as the audio" : "under its own audio"}`;
    return `${from}, with the text burned in and ${sound}. ${cover}`;
  }

  function draftCard(draft, canDecide, auto) {
    const isReel = draft.kind === "reel" && draft.reel;
    const images = !isReel || draft.reel.source === "photos" ? el("div", { class: "ig-slides" }, draft.slides.map((slide, i) => el("figure", {},
      el("img", {
        src: slideUrl(draft, i + 1),
        alt: slide.text, width: "200", height: "250", loading: "lazy",
      }),
      el("figcaption", {}, `${i + 1}. ${slide.text}`)))) : null;
    const slides = isReel ? el("div", {}, images, reelScript(draft.reel)) : images;
    const children = [
      slides,
      draft.note ? el("p", { class: "muted" }, `From your ${draft.kind === "reel" && !(draft.reel && draft.reel.source === "photos") ? "clip" : "photos"}. Your note: ${draft.note}`) : null,
      el("p", { class: "ig-caption" }, draft.caption),
      el("p", { class: "muted" }, draft.hashtags.length ? `${draft.hashtags.length} hashtags: ${draft.hashtags.join(" ")}` : "No hashtags"),
      keyValues([
        ["Status", draft.publishing ? "Posting now" : DRAFT_STATUS[draft.status] || draft.status],
        ["Category", draft.category],
        ["Review score (suggestion)", draft.review_score === null ? "Not scored" : `${draft.review_score}/100`],
        ["Reviewer notes", draft.review_notes || "None"],
        [auto ? "Publish time" : "Suggested publish time", when(auto ? draft.posts_at : draft.scheduled_for)],
      ]),
    ];
    const note = publishNote(draft, auto);
    if (note) children.push(note);
    if (draft.status === "pending" && draft.publishing) {
      // The publisher is mid-post; a decision would be refused.
    } else if (draft.status === "pending" && canDecide) {
      const approve = el("button", { type: "button", class: "primary" }, "Approve");
      const reject = el("button", { type: "button" }, "Reject");
      const status = el("p", { class: "muted", role: "status" });
      approve.addEventListener("click", () => decide(draft, "approve", [approve, reject], status));
      reject.addEventListener("click", () => decide(draft, "reject", [approve, reject], status));
      children.push(el("div", { class: "ig-actions" }, approve, reject), status);
      if (draft.kind === "reel" && draft.reel) children.push(holdToggle(draft, status, auto));
    } else if (draft.status === "pending") {
      children.push(muted("Approving or rejecting needs RECOMMEND permission."));
    } else if (draft.status === "approved" && !auto) {
      children.push(muted(draft.kind === "reel" && draft.reel && draft.reel.source === "photos"
        ? "Approved, but automatic posting is off. Download the slides to assemble a reel manually, or switch automatic posting on to publish the made reel and Story."
        : draft.kind === "reel"
        ? "Approved. Post it yourself: share your original clip as a reel, add the on-screen text above in Instagram's editor and copy the caption."
        : "Approved and saved as an example. Post it yourself: download the slides in order and copy the caption."));
      children.push(postKit(draft));
    } else if (draft.status === "published" && draft.media_id) {
      children.push(muted(`Posted to Instagram (media ${draft.media_id}).`));
    }
    return card(`${draft.created_on} · ${draft.topic}`, ...children);
  }

  const MAX_PHOTOS = 10;
  const MAX_SIDE = 2160;
  const UPLOAD_ERRORS = {
    photo_count: `Choose 1-${MAX_PHOTOS} photos.`,
    photo_too_large: "A photo is too large. Try a smaller one.",
    upload_too_large: "The photos are too large together. Try fewer at a time.",
    unsupported_photo: "Only JPEG and PNG photos can be added.",
    heic_unsupported: "That HEIC photo couldn't be converted on this device. Export it as JPEG and try again.",
    unreadable_photo: "A photo couldn't be read. Try exporting it again.",
    invalid_note: "Write a short note (up to 300 characters).",
    uploads_unavailable: "Photo uploads aren't switched on for this server yet.",
  };

  // Phones take large HEIC or JPEG photos; send a smaller JPEG when the browser can decode it. The server still
  // checks, strips metadata from and re-encodes every photo, whatever arrives.
  async function shrink(file) {
    try {
      const bitmap = await createImageBitmap(file);
      const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      const context = canvas.getContext("2d");
      context.fillStyle = "#fff";
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      bitmap.close();
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
      if (blob) return blob;
    } catch {
      // Fall back to the original file.
    }
    return file;
  }

  function musicChoice(data, id, withMix) {
    const tracks = data.music || [];
    const pick = el("select", { id: `${id}-music`, name: "music" },
      el("option", { value: "off" }, withMix ? "Off: keep the original audio" : "Off: no music"),
      el("option", { value: "agents", selected: "" }, "Let the agents pick from my library"),
      ...tracks.map((t) => el("option", { value: t.track_id }, t.title)));
    const mix = el("select", { id: `${id}-mix`, name: "mix" },
      el("option", { value: "under" }, "Mix it under the original audio"),
      el("option", { value: "replace" }, "Replace the original audio"));
    const node = el("div", {},
      el("label", { for: `${id}-music` }, tracks.length ? "Music (only tracks with a recorded licence)" : "Music (add licensed tracks to the music library first)"), pick,
      withMix ? el("label", { for: `${id}-mix` }, "Mix") : null, withMix ? mix : null);
    return { node, values: () => ({ music: pick.value, mix: withMix ? mix.value : "replace" }) };
  }

  function uploadCard(data, canDecide) {
    const intro = muted("Add 1-10 of your own photos and a short note, like \"Dishoom Kings Cross, lamb raan, 8/10\". The next daily run turns the oldest waiting set into the day's draft instead of an AI one; the note is the only source for the place, dish, rating or price.");
    if (!canDecide) return card("Add your photos", intro, muted("Adding photos needs RECOMMEND permission."));
    if (data.state !== "available") return card("Add your photos", intro, muted("Not available until the Instagram agent has run on this server."));
    if (!data.uploads_on) return card("Add your photos", intro, muted(UPLOAD_ERRORS.uploads_unavailable));
    const photos = el("input", { id: "ig-photos", type: "file", name: "photos", accept: "image/jpeg,image/png,image/heic,image/heif", multiple: "", required: "" });
    const note = el("textarea", { id: "ig-note", name: "note", rows: "3", maxlength: "300", required: "", placeholder: "Dishoom Kings Cross, lamb raan, 8/10" });
    const submit = el("button", { type: "submit", class: "primary" }, "Add photos");
    const status = el("p", { class: "muted", role: "status" });
    // Only Instagram gets the rendered video, so a photo reel needs automatic posting on.
    const renders = Boolean(data.renders && data.renders.photos && data.auto_publish);
    const reel = el("input", { id: "ig-as-reel", type: "checkbox", name: "reel", checked: "", disabled: "" });
    reel.checked = true;
    const music = musicChoice(data, "ig-photo", false);
    music.node.hidden = false;
    reel.addEventListener("change", () => { music.node.hidden = !reel.checked; });
    const form = el("form", { class: "ig-upload" },
      el("label", { for: "ig-photos" }, "Photos (1-10, JPEG or PNG; iPhone photos are converted)"), photos,
      el("label", { for: "ig-note" }, "Note"), note,
      el("label", { for: "ig-as-reel" }, reel, " Always a reel + Story (slow zooms, text burned in)"),
      renders ? null : muted(data.auto_publish ? "This server can't make reels yet (its ffmpeg lacks what's needed)." : "Photo reels need automatic posting switched on, since the video only goes to Instagram."), music.node,
      el("div", { class: "ig-actions" }, submit), status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const files = Array.from(photos.files || []);
      if (files.length < 1 || files.length > MAX_PHOTOS) { status.textContent = UPLOAD_ERRORS.photo_count; return; }
      if (!note.value.trim()) { status.textContent = UPLOAD_ERRORS.invalid_note; return; }
      submit.disabled = true;
      const signIn = signInCount;
      status.textContent = "Preparing photos…";
      const body = new FormData();
      body.append("note", note.value.trim());
      if (reel.checked) {
        body.append("reel", "1");
        for (const [key, value] of Object.entries(music.values())) body.append(key, value);
      }
      for (const [i, file] of files.entries()) body.append("photos", await shrink(file), `photo-${i + 1}.jpg`);
      if (signIn !== signInCount) return;
      status.textContent = "Uploading…";
      const result = await api("api/v1/instagram/uploads", { method: "POST", body });
      if (signIn !== signInCount) { if (result.ok) clearCache(); return; }
      if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
      if (!result.ok) {
        const code = (result.data && result.data.error) || result.status;
        status.textContent = `Could not add the photos. ${UPLOAD_ERRORS[code] || `(${code})`}`;
        submit.disabled = false;
        return;
      }
      clearCache();
      render();
    });
    return card("Add your photos", intro, form);
  }

  const MAX_CLIP_BYTES = 300 * 1024 * 1024;
  const CLIP_ERRORS = {
    clip_too_large: "The clip is over 300 MB. Export it at 1080p.",
    invalid_note: "Add a short note (up to 300 characters).",
    network_error: "The connection dropped. Try again.",
    storage_error: "The server couldn't save the clip (it may be out of space). Try again later.",
  };

  let unfinishedClip = null; // {file, clipId} whose finish reply was lost twice: the next try only finishes it

  async function finishClip(file, clipId, signIn) {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      if (signIn !== signInCount) return { ok: false, status: -1, data: null };
      const done = await api("api/v1/instagram/clips/finish", { method: "POST", body: { clip_id: clipId } });
      if (signIn !== signInCount) return { ok: false, status: -1, data: null }; // a newer session owns unfinishedClip
      if (done.status !== 0) { unfinishedClip = null; return done; }
    }
    unfinishedClip = { file, clipId }; // finishing again is safe; uploading again would queue it twice
    return { ok: false, status: 0, data: null };
  }

  async function sendClip(file, note, status, extra = {}) {
    const signIn = signInCount;
    if (unfinishedClip && unfinishedClip.file === file) {
      status.textContent = "Checking the clip against Instagram's video rules…";
      return finishClip(file, unfinishedClip.clipId, signIn);
    }
    const start = await api("api/v1/instagram/clips", { method: "POST", body: { note, size: file.size, ...extra } });
    if (!start.ok) return start;
    const { clip_id: clipId, chunk_bytes: chunk } = start.data;
    for (let offset = 0; offset < file.size; offset += chunk) {
      if (signIn !== signInCount) return { ok: false, status: -1, data: null };  // signed out meanwhile: stop sending
      status.textContent = `Uploading… ${Math.floor((offset / file.size) * 100)}%`;
      const part = file.slice(offset, Math.min(offset + chunk, file.size));
      let sent = await api(`api/v1/instagram/clips/chunk?clip=${clipId}&offset=${offset}`, { method: "POST", body: part });
      if (sent.status === 0 && signIn === signInCount) sent = await api(`api/v1/instagram/clips/chunk?clip=${clipId}&offset=${offset}`, { method: "POST", body: part });
      if (!sent.ok) return sent;
    }
    status.textContent = "Checking the clip against Instagram's video rules…";
    return finishClip(file, clipId, signIn);
  }

  function clipCard(data, canDecide) {
    const renders = Boolean(data.renders && data.renders.clip);
    const intro = muted(`Add one of your own clips (3-90 seconds, raw or edited) and a short note. The next daily run turns the oldest waiting clip into the day's reel; ${renders ? "the agents trim it and burn in the on-screen text" : "it posts as filmed"}. ${data.auto_publish ? "It publishes as a reel and Story automatically if it scores 70 or more, unless you reject it or hold it for your approval." : "Automatic posting is off, so it won't post on its own."} The note is the only source for the place, dish, rating or price.`);
    if (!canDecide) return card("Add a reel clip", intro, muted("Adding clips needs RECOMMEND permission."));
    if (data.state !== "available") return card("Add a reel clip", intro, muted("Not available until the Instagram agent has run on this server."));
    const clip = el("input", { id: "ig-clip", type: "file", name: "clip", accept: "video/mp4,video/quicktime", required: "" });
    const note = el("textarea", { id: "ig-clip-note", name: "note", rows: "3", maxlength: "300", required: "", placeholder: "Bar Italia Soho, espresso, 9/10" });
    const submit = el("button", { type: "submit", class: "primary" }, "Add clip");
    const status = el("p", { class: "muted", role: "status" });
    const problems = el("ul", { class: "muted" });
    const music = renders ? musicChoice(data, "ig-clip", true) : null;
    const form = el("form", { class: "ig-upload" },
      el("label", { for: "ig-clip" }, "Clip (MP4 or MOV, up to 300 MB, at most 1080 px wide)"), clip,
      el("label", { for: "ig-clip-note" }, "Note"), note, music ? music.node : null,
      el("div", { class: "ig-actions" }, submit), status, problems);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      problems.replaceChildren();
      const file = (clip.files || [])[0];
      if (!file) { status.textContent = "Choose a clip."; return; }
      if (file.size > MAX_CLIP_BYTES) { status.textContent = CLIP_ERRORS.clip_too_large; return; }
      if (!note.value.trim()) { status.textContent = CLIP_ERRORS.invalid_note; return; }
      submit.disabled = true;
      const signIn = signInCount;
      const result = await sendClip(file, note.value.trim(), status, music ? music.values() : {});
      if (signIn !== signInCount) { if (result.ok) clearCache(); return; }
      if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
      if (!result.ok) {
        const code = (result.data && result.data.error) || result.status;
        if (code === "clip_rejected") {
          status.textContent = "Instagram won't accept this clip as it is. Export it again (1080p, H.264, 30 fps works everywhere):";
          problems.replaceChildren(...(result.data.problems || []).map((p) => el("li", {}, p)));
        } else {
          status.textContent = `Could not add the clip. ${CLIP_ERRORS[code] || `(${code})`}`;
        }
        submit.disabled = false;
        return;
      }
      clearCache();
      render();
    });
    return card("Add a reel clip", intro, form);
  }

  const MUSIC_ERRORS = {
    licence_required: "Record the licence (for example \"Pixabay Content License\") before adding a track.",
    invalid_title: "Give the track a title (up to 120 characters).",
    unsupported_track: "Only MP3, M4A and WAV tracks can be added.",
    track_too_large: "The track is over 20 MB.",
    upload_too_large: "The track is over 20 MB.",
  };

  function musicCard(data, canDecide) {
    const intro = muted("Music for reels: only tracks you add here, each with the licence you have for it, can be used. Nothing is downloaded automatically. Free sources include the YouTube Audio Library and Pixabay Music; check each track's own licence allows Instagram use.");
    if (!canDecide || data.state !== "available") return card("Reel music library", intro, muted(canDecide ? "Not available until the Instagram agent has run on this server." : "Adding music needs RECOMMEND permission."));
    const list = el("ul", {}, (data.music || []).map((track) => {
      const remove = el("button", { type: "button" }, "Delete");
      const status = el("span", { class: "muted", role: "status" });
      remove.addEventListener("click", async () => {
        if (!window.confirm(`Delete "${track.title}"? Waiting uploads that chose it get no music. Reels already drafted with it but not yet made will fail and won't post.`)) return;
        remove.disabled = true;
        const signIn = signInCount;
        const result = await api("api/v1/instagram/music/delete", { method: "POST", body: { track_id: track.track_id } });
        if (signIn !== signInCount) { if (result.ok) clearCache(); return; }
        if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
        clearCache();
        if (result.ok) { render(); return; }
        status.textContent = ` Could not delete it (${(result.data && result.data.error) || result.status}).`;
        remove.disabled = false;
      });
      return el("li", {}, `${track.title}: ${track.licence}${track.source ? ` (${track.source})` : ""} `, remove, status);
    }));
    const file = el("input", { id: "ig-track", type: "file", name: "track", accept: "audio/mpeg,audio/mp4,audio/x-m4a,audio/wav", required: "" });
    const title = el("input", { id: "ig-track-title", name: "title", maxlength: "120", required: "" });
    const licence = el("input", { id: "ig-track-licence", name: "licence", maxlength: "300", required: "", placeholder: "Pixabay Content License" });
    const source = el("input", { id: "ig-track-source", name: "source", maxlength: "300", placeholder: "Where you got it, e.g. a link" });
    const submit = el("button", { type: "submit", class: "primary" }, "Add track");
    const status = el("p", { class: "muted", role: "status" });
    const form = el("form", { class: "ig-upload" },
      el("label", { for: "ig-track" }, "Track (MP3, M4A or WAV, up to 20 MB)"), file,
      el("label", { for: "ig-track-title" }, "Title"), title,
      el("label", { for: "ig-track-licence" }, "Licence"), licence,
      el("label", { for: "ig-track-source" }, "Source"), source,
      el("div", { class: "ig-actions" }, submit), status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const track = (file.files || [])[0];
      if (!track) { status.textContent = "Choose a track."; return; }
      if (!licence.value.trim()) { status.textContent = MUSIC_ERRORS.licence_required; return; }
      submit.disabled = true;
      const signIn = signInCount;
      const body = new FormData();
      body.append("title", title.value.trim());
      body.append("licence", licence.value.trim());
      body.append("source", source.value.trim());
      body.append("track", track, "track");
      status.textContent = "Uploading…";
      const result = await api("api/v1/instagram/music", { method: "POST", body });
      if (signIn !== signInCount) { if (result.ok) clearCache(); return; }
      if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
      if (!result.ok) {
        const code = (result.data && result.data.error) || result.status;
        status.textContent = `Could not add the track. ${MUSIC_ERRORS[code] || `(${code})`}`;
        submit.disabled = false;
        return;
      }
      clearCache();
      render();
    });
    return card("Reel music library", intro, list, form);
  }

  async function deleteClip(clip, button, status) {
    if (!window.confirm("Delete this clip? It won't be posted.")) return;
    button.disabled = true;
    const signIn = signInCount;
    const result = await api("api/v1/instagram/clips/delete", { method: "POST", body: { clip_id: clip.clip_id } });
    if (signIn !== signInCount) { if (result.ok) clearCache(); return; }
    if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
    if (!result.ok) {
      const gone = result.status === 404;
      status.textContent = gone ? "Already used or deleted. Refresh to see the queue." : `Could not delete it (${(result.data && result.data.error) || result.status}). Try again.`;
      button.disabled = gone;
      clearCache();
      return;
    }
    clearCache();
    render();
  }

  function waitingClipsCard(clips, canDecide) {
    if (!clips.length) return null;
    return card("Your clips waiting", muted(`Oldest first: the first clip becomes the next daily reel. ${clips.length} waiting.`),
      clips.map((clip, index) => {
        const status = el("p", { class: "muted", role: "status" });
        const remove = canDecide ? el("button", { type: "button" }, "Delete") : null;
        if (remove) remove.addEventListener("click", () => deleteClip(clip, remove, status));
        return el("div", { class: "ig-upload-item" },
          el("p", {}, el("strong", {}, index === 0 ? "Next: " : `${index + 1}. `), clip.note),
          el("p", { class: "muted" }, `${clip.seconds === null ? "Clip" : `${Math.round(clip.seconds)} s clip`} · added ${when(clip.created_at)}`),
          remove ? el("div", { class: "ig-actions" }, remove) : null, status);
      }));
  }

  const uploadPhotoUrl = (upload, n) => `api/v1/instagram/upload-photo?upload=${encodeURIComponent(upload.upload_id)}&n=${n}`;

  async function deleteUpload(upload, button, status) {
    if (!window.confirm("Delete these photos? They won't be posted.")) return;
    button.disabled = true;
    const signIn = signInCount;
    const result = await api("api/v1/instagram/uploads/delete", { method: "POST", body: { upload_id: upload.upload_id } });
    if (signIn !== signInCount) { if (result.ok) clearCache(); return; }
    if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
    if (!result.ok) {
      const gone = result.status === 404;
      status.textContent = gone ? "Already used or deleted. Refresh to see the queue." : `Could not delete them (${(result.data && result.data.error) || result.status}). Try again.`;
      button.disabled = gone;
      clearCache();
      return;
    }
    clearCache();
    render();
  }

  function waitingCard(uploads, canDecide) {
    if (!uploads.length) return null;
    return card("Your photos waiting", muted(`Oldest first: the first set becomes the next daily draft. ${uploads.length} waiting.`),
      uploads.map((upload, index) => {
        const status = el("p", { class: "muted", role: "status" });
        const remove = canDecide ? el("button", { type: "button" }, "Delete") : null;
        if (remove) remove.addEventListener("click", () => deleteUpload(upload, remove, status));
        return el("div", { class: "ig-upload-item" },
          el("p", {}, el("strong", {}, index === 0 ? "Next: " : `${index + 1}. `), upload.note),
          el("p", { class: "muted" }, `${upload.photos} photo${upload.photos === 1 ? "" : "s"} · added ${when(upload.created_at)}`),
          el("div", { class: "ig-slides ig-thumbs" }, Array.from({ length: upload.photos }, (_, i) =>
            el("img", { src: uploadPhotoUrl(upload, i + 1), alt: `Photo ${i + 1}`, width: "135", height: "169", loading: "lazy" }))),
          remove ? el("div", { class: "ig-actions" }, remove) : null, status);
      }));
  }

  function learningsCard(learned, next) {
    const { change, keep } = igContentFindings(learned);
    const findings = [change, keep].filter(Boolean);
    return card("What to test next", keyValues(Object.entries(next)),
      muted("Suggested tests, not proven causes. Hour and format use the selected filters; stored learnings below use their own 90-day sample."),
      findings.length ? el("details", {}, el("summary", {}, "Stored learnings behind the suggestion"), muted(learned.sample_note),
        el("ul", { class: "compact" }, findings.map((f) => el("li", {}, f.text)))) : null);
  }

  const igView = { section: "performance", days: "30", format: "all", metric: "views", checkpoint: "24h" };
  const IG_FORMATS = { reel: "Reel", story: "Story", post: "Post" };
  const IG_METRICS = { views: "Views", reach: "Accounts reached", follows: "Follows gained", profile_visits: "Profile visits",
    saved: "Saves", shares: "Shares", likes: "Likes", comments: "Comments", replies: "Replies" };
  const igNumber = (value) => typeof value === "number" && Number.isFinite(value);
  const igCount = (value) => igNumber(value) ? value.toLocaleString("en-GB", { maximumFractionDigits: 1 }) : "Unavailable";
  const igMedian = (values) => {
    const sorted = values.filter(igNumber).sort((a, b) => a - b);
    const middle = Math.floor(sorted.length / 2);
    return sorted.length ? (sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2) : null;
  };
  const igFormatCount = (format, count) => `${count} ${count === 1 ? format === "story" ? "Story" : IG_FORMATS[format].toLowerCase() : {reel: "reels", story: "Stories", post: "posts"}[format]}`;
  function igContentFindings(learned) {
    const features = new Set(["hook length", "caption length", "caption ask", "hashtag count", "hashtag", "reel length"]);
    return {
      change: (learned.not_working || []).find((f) => features.has(f.feature)),
      keep: (learned.working || []).find((f) => features.has(f.feature)),
    };
  }
  function igNextTest(measured, value, groups, metric, learned) {
    const formats = ["reel", "post"].map((format) => {
      const peers = measured.filter((p) => p.format === format);
      return { format, n: peers.length, median: igMedian(peers.map(value)) };
    }).filter((g) => g.n >= 5).sort((a, b) => b.median - a.median);
    const hour = groups.find((g) => g.values.length >= 5 && igMedian(g.values) > 0);
    // General learnings combine formats/checkpoints; do not turn a timing or format association
    // from that separate sample into a claim about the selected chart cohort.
    const { change, keep } = igContentFindings(learned);
    return {
      "Format to test": formats.length === 2 && formats[0].median > formats[1].median
        ? `${IG_FORMATS[formats[0].format]} leads on median ${metric.toLowerCase()} (${igCount(formats[0].median)}, n=${formats[0].n}); candidate for a separate format-only test, not a conversion verdict. Stories are separate.`
        : "No clear format winner: need at least 5 measured reels and 5 posts at this checkpoint, with different medians. Stories are separate.",
      "Posting hour": hour ? `${IG_FORMATS[hour.format]} at ${hour.hour}:00 London: median ${igCount(igMedian(hour.values))} ${metric.toLowerCase()}, n=${hour.values.length}. Candidate for a separate timing-only test; don't combine it with the content test below.`
        : "No dependable hour yet. Keep the current slot while collecting 5 matched results per hour; follow the single content test below.",
      "Next reel": change ? `Test an alternative to ${change.feature}: ${change.value}; keep timing and other content choices fixed.`
        : keep ? `Repeat ${keep.feature}: ${keep.value}; change only the opening hook, then compare the same checkpoint.`
        : "Test a clearer opening hook; keep timing and caption fixed. Compare reach and follows at the same checkpoint.",
      "Next Story": "Test first-frame wording; keep the slot fixed. Compare live reach and replies at matched capture ages, not against reels.",
    };
  }
  const igDate = (value) => new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", day: "numeric", month: "short" }).format(new Date(value));
  // Locale output is for display, never for parsing (en-CA is not ISO on every browser).
  const igDay = (value) => {
    const parts = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(value));
    const part = (type) => parts.find((p) => p.type === type).value;
    return `${part("year")}-${part("month")}-${part("day")}`;
  };
  const igWeek = (value) => {
    const monday = new Date(`${igDay(value)}T12:00:00Z`);
    monday.setUTCDate(monday.getUTCDate() - (monday.getUTCDay() + 6) % 7);
    return monday.toISOString().slice(0, 10);
  };
  const igLink = (item) => typeof item.permalink === "string" && item.permalink.startsWith("https://")
    ? el("a", { href: item.permalink, target: "_blank", rel: "noopener noreferrer" }, "Open on Instagram") : null;

  function igSelect(key, label, options, change) {
    const select = el("select", { id: `ig-filter-${key}` }, options.map(([value, text]) => {
      const option = el("option", { value }, text);
      option.selected = igView[key] === value;
      return option;
    }));
    select.addEventListener("change", () => { igView[key] = select.value; change(); });
    return el("div", {}, el("label", { for: `ig-filter-${key}` }, label), select);
  }

  // Visible values and descriptions are the accessible chart; the bars are decoration.
  function igBar(label, value, max, note, format = "reel", result = "") {
    return el("li", { class: `ig-bar-row ${result}` },
      el("div", { class: "ig-bar-label" }, el("span", {}, label), el("strong", {}, igCount(value))),
      igNumber(value) ? svg("svg", { viewBox: "0 0 400 12", class: `ig-bar ig-${format}`, "aria-hidden": "true", focusable: "false", preserveAspectRatio: "none" },
        svg("rect", { width: "400", height: "12", class: "ig-bar-track" }),
        svg("rect", { width: String(value / Math.max(1, max) * 400), height: "12" })) : null,
      el("p", { class: "muted" }, note));
  }

  function igPerformance(data) {
    const history = data.performance || { items: [], window_days: 90 };
    const takeaway = el("div", { "aria-live": "polite" });
    const content = el("div", { "aria-live": "polite" });
    const filters = el("div", { class: "ig-filters" },
      igSelect("days", "Published in", [["30", "Last 30 days"], ["90", "Last 90 days"]], draw),
      igSelect("format", "Format", [["all", "All formats"], ["reel", "Reels"], ["story", "Stories"], ["post", "Posts"]], draw),
      igSelect("metric", "Measure", Object.entries(IG_METRICS), draw),
      igSelect("checkpoint", "Report checkpoint", [["24h", "24-hour report"], ["72h", "72-hour report"]], draw));
    function draw() {
      const now = Date.now();
      const since = now - Number(igView.days) * 86400000;
      const items = history.items.filter((p) => Date.parse(p.posted_at) >= since && (igView.format === "all" || p.format === igView.format));
      const snapshot = (p) => p.snapshots[igView.checkpoint];
      const value = (p) => snapshot(p) && snapshot(p)[igView.metric];
      const measured = items.filter((p) => igNumber(value(p)));
      const metric = IG_METRICS[igView.metric];
      const stat = (heading, number, note) => el("div", {}, el("span", { class: "muted" }, heading),
        el("strong", { class: "big-number" }, number), el("span", { class: "muted" }, note));
      const stats = el("div", { class: "ig-stats" }, stat("Published", String(items.length), "reels, Stories and posts"),
        stat(`${metric} · median`, igCount(igMedian(measured.map(value))), `${measured.length} measured / ${items.length} published`),
        stat("Missing this measure", String(items.length - measured.length), "not zero, not a failed post"));

      const weeks = new Map();
      // ISO Monday buckets in London, rather than the browser's timezone.
      for (let time = since; time <= now; time += 86400000) {
        const date = new Date(time);
        weeks.set(igWeek(date), { reel: 0, story: 0, post: 0 });
      }
      for (const item of items) {
        const week = weeks.get(igWeek(item.posted_at));
        if (week) week[item.format]++;
      }
      const totals = [...weeks.values()].map((w) => w.reel + w.story + w.post);
      const maxWeek = Math.max(1, ...totals);
      const cadence = card("When content went live", muted("Weekly counts · London time. Edge weeks cover only the selected period; reel and matching Story count separately."),
        el("ul", { class: "ig-bars" }, [...weeks].map(([week, counts]) => {
          let x = 0;
          return el("li", { class: "ig-bar-row" }, el("div", { class: "ig-bar-label" },
            el("span", {}, `Week of ${igDate(`${week}T12:00:00Z`)}`),
            el("strong", {}, String(counts.reel + counts.story + counts.post))),
          svg("svg", { viewBox: "0 0 400 12", class: "ig-bar", "aria-hidden": "true", focusable: "false", preserveAspectRatio: "none" },
            svg("rect", { width: "400", height: "12", class: "ig-bar-track" }),
            ...Object.keys(IG_FORMATS).map((format) => {
              const width = counts[format] / maxWeek * 400;
              const rect = svg("rect", { x: String(x), width: String(width), height: "12", class: `ig-${format}` });
              x += width;
              return rect;
            })),
          el("p", { class: "muted" }, Object.keys(IG_FORMATS).map((format) => igFormatCount(format, counts[format])).join(" · ")));
        })));

      function comparison(p) {
        if (!igNumber(value(p))) return ["Not measured", ""];
        if (p.format === "story") return ["Live capture; not graded against reels or other Story ages", ""];
        const peers = measured.filter((other) => other.format === p.format);
        const median = igMedian(peers.map(value));
        if (peers.length < 3 || !median) return ["Need 3 measured peers and a non-zero median to compare", ""];
        const lift = value(p) / median;
        const label = lift >= 1.2 ? "Above typical" : lift <= 0.8 ? "Below typical" : "Typical";
        return [`${label} · ${lift.toFixed(1)}× ${IG_FORMATS[p.format].toLowerCase()} median · n=${peers.length}`, lift >= 1.2 ? "ig-above" : lift <= 0.8 ? "ig-below" : ""];
      }
      const max = Math.max(1, ...measured.map(value));
      const recent = card(`${metric} by published content`, muted(`Newest 12 shown. “Above / below typical” means at least 20% from the same-format median at this checkpoint, not a verdict on conversion.`),
        items.length ? el("ul", { class: "ig-bars" }, items.slice(0, 12).map((p) => {
          const [label, result] = comparison(p);
          return igBar(`${igDate(p.posted_at)} · ${IG_FORMATS[p.format]} · ${p.topic}`, value(p), max,
            `${label}${snapshot(p) ? ` · Captured ${when(snapshot(p).captured_at)}` : " · Report pending or metric unavailable"}`, p.format, result);
        })) : muted("No published media in this selection. Try 90 days or a different format."));
      const byFormat = card(`Which format earns ${metric.toLowerCase()}?`, muted("Median per published item, not total volume. Different formats serve different goals; Stories use live captures of varying ages."),
        el("ul", { class: "ig-bars" }, Object.entries(IG_FORMATS).filter(([fmt]) => igView.format === "all" || igView.format === fmt).map(([fmt, label]) => {
          const peers = measured.filter((p) => p.format === fmt);
          return igBar(label, igMedian(peers.map(value)), max, `${peers.length} measured · ${peers.length < 5 ? "small sample, test rather than conclude" : "observed association"}`, fmt);
        })));
      const hours = new Map();
      for (const p of measured.filter((p) => p.format !== "story")) {
        const hour = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", hour: "2-digit", hourCycle: "h23" }).format(new Date(p.posted_at));
        const key = `${p.format}:${hour}`;
        if (!hours.has(key)) hours.set(key, { format: p.format, hour, values: [] });
        hours.get(key).values.push(value(p));
      }
      const groups = [...hours.values()].sort((a, b) => igMedian(b.values) - igMedian(a.values));
      const learned = data.learnings || {};
      takeaway.replaceChildren(learningsCard(learned, igNextTest(measured, value, groups, metric, learned)));
      const timing = card(`Posting hours to test`, muted(`London time · median ${metric.toLowerCase()} per item, grouped by format and hour. Not proof of a best time: topic and hook also differ. Stories excluded because capture ages vary.`),
        groups.length ? el("ul", { class: "ig-bars" }, groups.slice(0, 6).map((g) => igBar(`${IG_FORMATS[g.format]} · ${g.hour}:00–${g.hour}:59`, igMedian(g.values), max,
          `n=${g.values.length} · ${g.values.length < 5 ? "small sample; collect more before changing the schedule" : "candidate for a controlled timing test"}`, g.format))) : muted("No measured reels or posts for this selection yet."));
      content.replaceChildren(...[stats,
        muted(`${history.last_synced_at ? `Last media sync ${when(history.last_synced_at)}.` : "No media sync recorded."} Stored data only; refreshing this page does not fetch Instagram insights.${history.truncated ? ` Showing only the newest ${history.limit} items.` : ""}`),
        el("details", { class: "ig-method" }, el("summary", {}, "How to read these results"),
          muted("24h and 72h select separate report checkpoints, never added together. Capture times show when values were actually read; delayed reports may be older than the nominal checkpoint. Stories can reuse the same live snapshot at both checkpoints. Missing/unsupported metrics stay unavailable."),
          muted("Follows gained are only the follows Instagram attributes to individual media, where supported—not total followers or net account growth. Views are not unique people or sales. A zero is shown only when it was measured.")),
        cadence, recent, el("div", { class: "ig-chart-grid" }, byFormat, timing)].filter(Boolean));
    }
    draw();
    return el("div", {}, takeaway, card("Explore performance", filters), content);
  }

  function igPublished(data, canDecide) {
    const items = (data.performance && data.performance.items) || [];
    const body = el("div", {});
    let shown = 0;
    const more = el("button", { type: "button" }, "Show more published content");
    function showMore() {
      for (const item of items.slice(shown, shown + 20)) {
        const reports = Object.entries(item.snapshots).map(([checkpoint, values]) => el("div", {},
          el("h3", {}, `${checkpoint} report · captured ${when(values.captured_at)}`),
          keyValues(Object.entries(IG_METRICS).map(([key, label]) => [label, igCount(values[key])]))));
        const draft = data.drafts.find((d) => d.draft_id === item.draft_id);
        body.append(card(`${IG_FORMATS[item.format]} · ${item.topic}`, muted(`Live ${when(item.posted_at)}${item.time_basis === "publish_record" ? " (publishing record; awaiting media sync)" : ""}`),
          igLink(item), el("details", {}, el("summary", {}, "Results and content"),
            reports.length ? reports : muted("No checkpoint report yet. This does not mean the content failed."),
            item.format === "story" ? muted("Story figures are the latest captured while live, not a full 72-hour observation.") : null,
            draft && item.format !== "story" ? draftCard(draft, canDecide, data.auto_publish) : null)));
      }
      shown += 20;
      more.hidden = shown >= items.length;
    }
    more.addEventListener("click", showMore);
    showMore();
    const unpublishedHistory = data.drafts.filter((d) => d.status === "published" && !items.some((p) => p.draft_id === d.draft_id));
    return el("div", {}, card("Published", muted("Reels, Stories and posts in the stored 90-day history. Open an item for its checkpoint metrics and content.")), body,
      !items.length ? muted("No published media captured yet.") : null, more,
      unpublishedHistory.length ? el("details", {}, el("summary", {}, "Other published drafts (no synced media in this window)"),
        unpublishedHistory.map((d) => draftCard(d, canDecide, data.auto_publish))) : null);
  }

  function igCalendar(entries, canDecide, showReview) {
    const content = el("div", {}, card("Content calendar",
      muted("Monday, Wednesday and Friday · London time. To add photos or a clip for a slot, include its pillar name in the upload note.")));
    if (!entries.length) content.append(muted("The calendar will appear after the next Instagram run."));
    for (const entry of entries) {
      const feedback = el("p", { role: "status", class: "muted", hidden: "" });
      const advance = entry.next_status && canDecide ? el("button", { type: "button" }, `Mark ${entry.next_status}`) : null;
      if (advance) advance.addEventListener("click", async () => {
        advance.disabled = true;
        const result = await api("api/v1/instagram/calendar/advance", {
          method: "POST", body: { id: entry.id, status: entry.status }
        });
        if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
        if (result.ok) { clearCache(); render(); return; }
        feedback.hidden = false;
        feedback.textContent = result.status === 409 ? "This entry changed. Refresh to see its status." : "Could not save the status.";
        advance.disabled = false;
      });
      const review = entry.needs_approval && canDecide ? el("button", { type: "button" }, "Go to awaiting review") : null;
      if (review) review.addEventListener("click", showReview);
      content.append(card(entry.pillar_name, muted(`${when(entry.slot)} · ${entry.status}`),
        entry.topic ? el("p", {}, entry.topic) : null,
        entry.scheduled_for && Date.parse(entry.scheduled_for) !== Date.parse(entry.slot)
          ? muted(`Moved to ${when(entry.scheduled_for)}`) : null,
        advance, review, feedback));
    }
    return content;
  }

  async function instagram(view) {
    const data = await load("api/v1/instagram/drafts");
    if (!data) return;
    const canDecide = Boolean(session) && ["recommend", "execute"].includes(session.permission);
    const needsReview = (d) => d.status === "pending" && !d.publishing
      && (!data.auto_publish || (d.reel && d.reel.hold) || !igNumber(d.review_score) || d.review_score < 70);
    const review = data.drafts.filter(needsReview);
    const drafts = data.drafts.filter((d) => ["pending", "approved", "failed"].includes(d.status) && !needsReview(d));
    const sections = [["performance", "Performance"], ["calendar", "Calendar"], ["review", `Awaiting review (${review.length})`], ["drafts", `Drafts (${drafts.length})`],
      ["published", "Published"], ["uploads", "Uploads & music"], ["activity", "Activity & audit"]];
    const panels = new Map();
    const buttons = new Map();
    const nav = el("div", { class: "ig-sections", role: "group", "aria-label": "Instagram sections" });
    const root = el("div", { class: "ig-dashboard" },
      card("@instafoodielove", muted(`${data.auto_publish ? "Automatic posting on" : "Automatic posting off"} · ${review.length} awaiting review · All times London`)), nav);
    // Lazy panels close over this snapshot. DOM equality alone cannot detect hidden queue changes.
    root.igSnapshot = JSON.stringify(data);
    if (data.state !== "available") root.append(card("Instagram data unavailable", muted(data.state === "not_configured"
      ? "No Instagram database is connected yet. Graphs will appear after media and reports are captured." : "The Instagram database could not be read. Empty graphs are not evidence of zero performance.")));
    const tokenWarning = data.token && ["expired", "expiring", "refresh_failed", "not_configured"].includes(data.token.state) ? tokenCard(data.token) : null;
    if (tokenWarning) root.append(tokenWarning);
    root.append(el("div", { id: "ig-hold-notices" }, ...holdFailureCards()));
    const fills = {
      performance: () => igPerformance(data),
      calendar: () => igCalendar(data.calendar || [], canDecide, () => show("review")),
      review: () => el("div", {}, card("Awaiting review", muted("Held or unapproved drafts that will not post automatically. Approve or reject below.")),
        review.length ? review.map((d) => draftCard(d, canDecide, data.auto_publish)) : muted("Nothing needs a decision.")),
      drafts: () => el("div", {}, card("Drafts, scheduled & failed", muted("Failed attempts stay here with their last error. Check Instagram before taking any further action; publishing is never retried here."), muted(data.auto_publish
        ? "Ready or approved drafts. Each card shows whether and when it will post; reject a pending draft to stop it."
        : "Automatic posting is off. Approved drafts stay here until posted manually.")),
        drafts.length ? drafts.map((d) => draftCard(d, canDecide, data.auto_publish)) : muted("No drafts waiting to publish.")),
      published: () => igPublished(data, canDecide),
      uploads: () => el("div", {}, uploadCard(data, canDecide), waitingCard(data.uploads || [], canDecide),
        clipCard(data, canDecide), waitingClipsCard(data.clips || [], canDecide), musicCard(data, canDecide)),
      activity: () => el("div", {}, tokenCard(data.token), card("Daily generation runs",
        muted("Drafts run on planned posting days. Skipped ticks are normal outside those slots or when a draft already exists."),
        table([["When", (r) => when(r.started_at)], ["Result", (r) => r.outcome], ["Drafts", (r) => r.drafts], ["Detail", (r) => r.error || "—"]], data.runs)),
        el("details", {}, el("summary", {}, "Rejected drafts"), data.drafts.filter((d) => d.status === "rejected").map((d) => draftCard(d, canDecide, data.auto_publish))))
    };
    for (const [id, label] of sections) {
      const button = el("button", { type: "button", "aria-controls": `ig-section-${id}`, "aria-pressed": "false" }, label);
      const panel = el("section", { id: `ig-section-${id}`, "aria-label": label, hidden: "" });
      buttons.set(id, button);
      panels.set(id, panel);
      button.addEventListener("click", () => show(id));
      nav.append(button);
      root.append(panel);
    }
    const filled = new Set();
    function show(id) {
      igView.section = id;
      if (tokenWarning) tokenWarning.hidden = id === "activity";
      for (const [key, panel] of panels) {
        panel.hidden = key !== id;
        buttons.get(key).setAttribute("aria-pressed", String(key === id));
      }
      if (!filled.has(id)) {
        filled.add(id);
        const panel = panels.get(id);
        panel.append(fills[id]());
        if (id === "activity") auditCard().then((audit) => { if (audit) panel.append(audit); });
      }
    }
    // Recover ALL saved changes without constructing hidden draft panels. In particular, an
    // unanswered release in Review must not prevent a saved hold in Drafts from being sent.
    const pendingHoldDrafts = canDecide ? data.drafts.filter((draft) => draft.kind === "reel"
      && draft.status === "pending" && !draft.publishing && draft.reel && pendingHold(draft)) : [];
    if (pendingHoldDrafts.length) {
      const recovery = card("Confirming saved hold changes");
      root.append(recovery);
      for (const draft of pendingHoldDrafts) {
        const pending = pendingHold(draft);
        const line = el("p", { class: "muted", role: "status" });
        const update = (message) => { line.textContent = `${draft.topic || draft.draft_id}: ${message}`; };
        update("Confirming an earlier hold change…");
        recovery.append(line);
        sendHold(draft, pending.body, pending.token, update).then((result) => { if (result) update(result.message); });
      }
    }
    const pendingHoldDraft = pendingHoldDrafts[0];
    show(pendingHoldDraft ? needsReview(pendingHoldDraft) ? "review" : "drafts" : igView.section);
    view.append(root);
  }


  const OUTCOME_LABELS = { ok: "Done", failed: "Failed", info: "Info" };
  const OUTCOME_STATUS = { ok: "operational", failed: "failed", info: "unverified" };
  // Only stored https links become anchors; anything else is shown as plain text.
  const notificationLink = (link) => (typeof link === "string" && link.startsWith("https://")
    ? el("a", { href: link, rel: "noopener noreferrer", target: "_blank" }, "Open")
    : "None");
  const inboxView = { page: 1 };
  async function inbox(view) {
    const body = el("div", {});
    view.append(card("Inbox", muted("Notifications from your agents and scheduled jobs, newest first. Times are London time."), body));
    let generation = 0;
    async function show() {
      const ticket = ++generation; // a later page choice supersedes this request
      const epoch = cacheEpoch;
      const result = await api(`api/v1/notifications?page=${inboxView.page}&per_page=25`);
      if (epoch !== cacheEpoch || ticket !== generation) return;
      if (result.status === 401) { showLogin("Your session has ended. Sign in again."); return; }
      if (!result.ok) { body.replaceChildren(muted(`The inbox could not be loaded (${(result.data && result.data.error) || result.status}).`)); return; }
      const data = result.data;
      if (data.state !== "available") {
        body.replaceChildren(muted(data.state === "not_configured" ? "No notifications yet." : "The inbox is unavailable right now."));
        return;
      }
      const prev = el("button", { type: "button" }, "Previous");
      const next = el("button", { type: "button" }, "Next");
      prev.disabled = data.page <= 1;
      next.disabled = data.page >= data.pages;
      prev.addEventListener("click", () => { inboxView.page = Math.max(1, data.page - 1); show(); });
      next.addEventListener("click", () => { inboxView.page = data.page + 1; show(); });
      body.replaceChildren(
        table([["When", (r) => when(r.created_at)], ["Source", (r) => r.source],
          ["Outcome", (r) => el("span", { class: `status status-${OUTCOME_STATUS[r.outcome] || "unverified"}` }, OUTCOME_LABELS[r.outcome] || r.outcome)],
          ["What happened", (r) => r.title], ["Link", (r) => notificationLink(r.link)]], data.entries, "No notifications yet."),
        el("div", { class: "pager" }, prev,
          el("span", { "aria-live": "polite" }, `Page ${data.page} of ${data.pages} · ${data.total} notification${data.total === 1 ? "" : "s"}`), next));
    }
    await show();
  }

  const VIEWS = { overview, inbox, "my-day": myDay, energy, agents: agentsView, integrations, automations: automationsView, settings, instagram };

  let renderCount = 0;
  // A background render runs after fresh data replaced what the section was showing; `notice` reports a partial refresh.
  async function render(options) {
    if (!session) return;
    const background = Boolean(options && options.background);
    const id = (location.hash || "#overview").slice(1);
    const section = SECTIONS.find(([key]) => key === id) || SECTIONS[0];
    for (const link of document.querySelectorAll("#nav-list a")) {
      if (link.dataset.section === section[0]) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    if (background && section[0] === "energy" && energyRefreshOwner !== null) return;
    const current = ++renderCount;
    renderUses = new Map();
    const uses = renderUses;
    if (!background) {
      $("view-title").textContent = section[1];
      $("view-status").textContent = "";
    }
    // Cached data resolves within microtasks, so this only fires when the section waits on the network.
    const loading = background ? 0 : setTimeout(() => {
      if (current !== renderCount) return;
      $("view").replaceChildren();
      $("view-status").textContent = "Loading…";
    }, 0);
    const view = document.createDocumentFragment();
    await (VIEWS[section[0]] ? VIEWS[section[0]](view, { background }) : placeholder(view, section[0]));
    clearTimeout(loading);
    if (current !== renderCount) return;
    if (background && section[0] === "energy" && energyRefreshOwner !== null) return; // a refresh began while this render loaded
    const editing = document.activeElement && $("view").contains(document.activeElement) && document.activeElement.matches("input, textarea, select");
    // Unsent uploads and changed music choices would be lost if the form were rebuilt.
    const unsent = [...$("view").querySelectorAll("form.ig-upload input[type=file], form.ig-upload textarea, form.ig-upload select")]
      .some((field) => field.matches("select")
        ? field.value !== ([...field.options].find((option) => option.defaultSelected) || field.options[0]).value
        : (field.files ? field.files.length > 0 : field.value.trim() !== ""));
    if (background && (editing || unsent)) return;
    const fresh = [...view.childNodes];
    const title = $("view-title").textContent;
    const previous = sectionViews.get(section[0]);
    // Reuse the nodes already built for identical content: images stay loaded and nothing re-flows.
    const same = previous && previous.title === title && previous.status === $("view-status").textContent
      && previous.nodes.length === fresh.length && previous.nodes.every((node, i) => node.igSnapshot === fresh[i].igSnapshot && node.isEqualNode(fresh[i]));
    const nodes = same ? previous.nodes : fresh;
    const shown = $("view").childNodes;
    if (shown.length !== nodes.length || nodes.some((node, i) => shown[i] !== node)) {
      if (same) for (const node of nodes) if (node.querySelectorAll) for (const input of node.querySelectorAll('input[type="password"]')) input.value = "";
      $("view").replaceChildren(...nodes);
    }
    if ($("view-status").textContent === "Loading…") $("view-status").textContent = "";
    if (options && options.notice) $("view-status").textContent = options.notice;
    sectionViews.set(section[0], { nodes, title, status: $("view-status").textContent });
    if (section[0] !== "overview" && $("view-title").textContent !== section[1]) $("view-title").textContent = section[1];
    $("nav").classList.remove("open");
    $("menu-toggle").setAttribute("aria-expanded", "false");
    if (!background) revalidate(current, uses);
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
    $("passkey-signin").addEventListener("click", passkeySignIn);
    $("hello-jarvis").addEventListener("click", helloJarvis);
    const closeMenu = (refocus) => {
      if (!$("nav").classList.contains("open")) return;
      $("nav").classList.remove("open");
      $("menu-toggle").setAttribute("aria-expanded", "false");
      if (refocus) $("menu-toggle").focus();
    };
    $("menu-toggle").addEventListener("click", () => {
      const open = $("nav").classList.toggle("open");
      $("menu-toggle").setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeMenu($("nav").contains(document.activeElement)); });
    document.addEventListener("click", (event) => {
      if ($("menu-toggle").contains(event.target) || ($("nav").contains(event.target) && !event.target.closest("a"))) return;
      closeMenu(false);
    });
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(syncTopbarHeight).observe(document.querySelector(".topbar"));
    else window.addEventListener("resize", syncTopbarHeight);
    window.addEventListener("hashchange", render);
    const result = await api("api/v1/session");
    if (result.ok) showApp(result.data);
    else showLogin(result.status === 0 ? LOGIN_ERRORS.network_error : "");
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
