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
    clearCache();
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

  function showApp(info) {
    session = info; csrf = info.csrf_token;
    $("login-view").hidden = true;
    $("app-view").hidden = false;
    $("permission-badge").textContent = `Permission: ${info.permission.toUpperCase()}`;
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
    playVault("unlock");
    speak(VAULT.unlock.caption, reducedMotion() ? 0 : VAULT_GRANTED_MS);
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
    "api/v1/automations", "api/v1/integrations", "api/v1/audit", "api/v1/passkeys", "api/v1/overview"];
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

  function svg(tag, attrs, ...children) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
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

  function alertItem(a) {
    return el("li", { class: `alert alert-${a.severity}` },
      el("strong", {}, a.severity === "action" ? "ACT NOW" : a.severity.toUpperCase()), " ", a.fact,
      a.href ? [" ", el("a", { href: a.href }, "Open")] : null);
  }

  function instagramWidget(ig) {
    const latest = ig.latest_attempt;
    return widget("Instagram", "Daily drafts", {},
      el("p", {}, status(ig.status)),
      el("p", { class: "big-number" }, String(ig.pending_drafts), el("span", { class: "muted" }, ` draft${ig.pending_drafts === 1 ? "" : "s"} awaiting review`)),
      latest ? keyValues([["Last drafting run", when(latest.started_at)], ["Result", latest.outcome]]) : muted(ig.summary),
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
    const energyState = data.energy.state === "available" ? "operational" : data.energy.state;
    const latestRun = data.briefing.latest_run;
    view.append(el("div", { class: "widgets" },
      reactor(data),
      widget("Needs attention", "Facts from source health checks", { span: 2 },
        data.attention.length === 0 ? muted("Nothing needs attention.") :
          el("ul", { class: "alerts" }, data.attention.slice(0, 6).map(alertItem)),
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
      instagramWidget(data.instagram),
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
  // Energy -----------------------------------------------------------------------------------
  // The pure helpers (validateData, viewGroups, anomalyModel, chartLayout, ...) are loaded from
  // static/energy-core.js: the same script that runs the standalone /energy-usage dashboard.
  const ENERGY_RELOAD_MS = 5 * 60 * 1000;
  const COST_REASONS = { missing_unit_rate: "missing unit rates", missing_standing_charge: "missing standing charges", unknown_gas_units: "unverified gas units" };

  const energyMoney = (value) => (value === null ? "Cost unavailable"
    : value > 0 && value < 0.005 ? "Under £0.01 estimated cost" : `£${value.toFixed(2)} estimated cost`);
  const energyTime = (value) => (value ? new Date(value).toLocaleString("en-GB", { timeZone: "Europe/London" }) : "unavailable");
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
  function energyDashboard(root, published, generation) {
    let data = null, { fuel, period } = energyChoice, busy = false, loadError = false;
    let refreshBusy = false, lastRefreshStatus = null, stopped = false, attached = false;
    // Unique per instance, so render() never swaps this live dashboard for an older, stopped copy.
    root.dataset.instance = String(++energyInstances);
    let repositionTooltip = () => {};
    const canRefresh = Boolean(session) && ["recommend", "execute"].includes(session.permission);

    const statusLine = el("p", { class: "energy-status", role: "status", "aria-live": "polite", "data-state": "loading" }, "Loading dashboard data…");
    const updated = el("p", { class: "muted energy-updated" }, "Data as of: electricity unavailable · gas unavailable · last successful sync unavailable.");
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
    const notice = muted("");

    const setStatus = (state, message) => { statusLine.dataset.state = state; statusLine.textContent = message; };

    root.append(
      el("section", { class: "card energy-head", "aria-label": "Energy data status" },
        el("div", { class: "energy-head-row" },
          el("div", {}, el("p", { class: "energy-eyebrow" }, "Read-only Octopus intelligence"), statusLine, updated),
          el("div", { class: "energy-refresh" }, refreshButton, canRefresh ? null
            : muted(`Fetch latest data needs RECOMMEND permission; this session is ${session ? session.permission.toUpperCase() : "signed out"}.`))),
        refreshStatus,
        muted("Fetch latest data requests a protected background sync. Meter readings can lag by 1–2 days. "
          + "This snapshot also reloads every 5 minutes; neither mode is a live meter feed.")),
      el("section", { class: "card energy-filters", "aria-label": "Chart filters" },
        el("div", { class: "energy-filter" }, el("h3", {}, "Fuel"), el("div", { class: "segmented" }, fuelButtons)),
        el("div", { class: "energy-filter" }, el("h3", {}, "Period"), el("div", { class: "segmented" }, periodButtons))),
      cards,
      el("section", { class: "card energy-chart-card" }, title, range,
        el("p", { id: "energy-chart-help", class: "muted energy-small" }, "Hover or tap a point for its recorded period. "
          + "Keyboard: Tab to a point, arrows to move, Home/End for endpoints, Enter/Space to show, Escape to dismiss. "
          + "Ringed points are unusually high; gaps mean usage is unavailable. Scroll the chart horizontally on small screens."),
        stage,
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
          + "Hourly costs cover usage only and exclude standing charge; missing unit rates remain unavailable.")),
      el("div", { class: "grid energy-guidance" },
        el("section", { class: "card" }, el("h2", {}, "Suggested next action"), action),
        el("section", { class: "card" }, el("h2", {}, "Confirmed Octopus events only"),
          muted("No live event feed is connected. For an invited Power Down session, delay flexible demand such as laundry or EV charging. "
            + "For Power Up or Happy Hour, move flexible demand into the exact window confirmed in your Octopus account. "
            + "Never turn off medical, frost-protection or essential devices. Do not increase usage just to chase a reward."),
          el("a", { href: "https://octopus.energy/saving-sessions/", target: "_blank", rel: "noopener noreferrer" }, "Octopus event guidance")),
        el("section", { class: "card" }, el("h2", {}, "Device insight"), notice)),
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
      updated.textContent = `Data as of · Latest electricity reading ${energyTime(data.latest_reading_at?.electricity)} · Latest gas reading ${energyTime(data.latest_reading_at?.gas)} · Last successful sync ${energyTime(newest(lastRefreshStatus?.last_success_at, data.last_successful_sync_at))} · Snapshot exported ${energyTime(data.generated_at)} · Europe/London`;
      notice.textContent = data.notice || "Whole-home meter data cannot identify individual appliances.";
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
        const result = await api("api/v1/energy/dashboard", { signal: controller.signal });
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
        const result = await api(path, { ...options, signal: controller.signal });
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
      refreshButton.disabled = true;
      const message = (state, text) => { refreshStatus.dataset.state = state; refreshStatus.textContent = text; };
      const finish = (state, text) => {
        refreshBusy = false;
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
          if (stopped || !live()) { refreshBusy = false; return; }
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

  async function energy(view) {
    const generation = renderCount; // the render this view belongs to, read before awaiting
    const [data, published] = await Promise.all([load("api/v1/energy"), load("api/v1/energy/dashboard")]);
    if (!data || generation !== renderCount) return;
    const h = data.health;
    const dashboard = el("div", { class: "energy-dashboard" });
    if (published) energyDashboard(dashboard, published, generation);
    else dashboard.append(card("Energy dashboard", el("p", { class: "energy-status", "data-state": "error" },
      "The published dashboard snapshot could not be loaded. Source records below are still shown; reload to retry.")));
    view.append(dashboard,
      el("h2", { class: "energy-section-title" }, "Source records"),
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
    const [data, jobs, agentData] = await Promise.all([load("api/v1/audit"), load("api/v1/automations"), load("api/v1/agents")]);
    if (!data || !jobs || !agentData || !session) return;
    const canDecide = ["recommend", "execute"].includes(session.permission);
    const passkeys = await passkeyCard();
    view.append(
      card("Session", keyValues([["Permission", session.permission.toUpperCase()], ["Session expires", when(session.expires_at)],
        session.remembered ? ["Remembered device", "Yes, until the session expires or you sign out"]
          : ["Idle timeout", `${Math.round(session.idle_timeout_seconds / 60)} minutes`]]),
      el("ul", {}, el("li", {}, "READ: retrieve and analyse data."),
        el("li", {}, "RECOMMEND: suggest changes and prepare drafts."),
        el("li", {}, "EXECUTE: perform explicitly approved operations. No execute operations exist yet."))),
      passkeys,
      card("Agents and automations",
        muted("These settings are read-only here. Schedules and agent definitions change through the repository; enable, disable and run-now controls arrive in Phase 5."),
        table([["Automation", (r) => r.name], ["Status", (r) => status(r.status)], ["Trigger", (r) => r.trigger]], jobs.automations, "No automations."),
        table([["Agent", (r) => r.name], ["Guardrail", (r) => r.guardrail], ["Definition", (r) => el("code", {}, r.definition)]], agentData.agents, "No agents."),
        keyValues([["Instagram draft approvals", canDecide ? "You can approve or reject drafts on the Instagram page." : "Read-only: your permission cannot approve drafts."]])),
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

  const DRAFT_STATUS = {
    pending: "Awaiting review", approved: "Approved", rejected: "Rejected", published: "Published", failed: "Failed",
  };

  const DECISION_ERRORS = {
    publishing: "It's being posted to Instagram now, so it can no longer be approved or rejected.",
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

  function publishNote(draft, auto) {
    if (draft.publishing) return muted("Posting to Instagram now. It can no longer be approved or rejected.");
    if (draft.publish_error) return muted(`Last publish attempt failed: ${draft.publish_error}`);
    if (!auto) return null;
    if (draft.will_post && draft.posts_at) return muted(`Posts to Instagram automatically at ${when(draft.posts_at)} unless you reject it before then.`);
    if (draft.status === "pending") return muted("Scored under 70, so it only posts if you approve it.");
    return null;
  }

  function draftCard(draft, canDecide, auto) {
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
    } else if (draft.status === "pending") {
      children.push(muted("Approving or rejecting needs RECOMMEND permission."));
    } else if (draft.status === "approved" && !auto) {
      children.push(muted("Approved and saved as an example. Post it yourself: download the slides in order and copy the caption."));
      children.push(postKit(draft));
    } else if (draft.status === "published" && draft.media_id) {
      children.push(muted(`Posted to Instagram (media ${draft.media_id}).`));
    }
    return card(`${draft.created_on} · ${draft.topic}`, ...children);
  }

  async function instagram(view) {
    const data = await load("api/v1/instagram/drafts");
    if (!data) return;
    const canDecide = Boolean(session) && ["recommend", "execute"].includes(session.permission);
    const last = data.runs[0];
    view.append(card("Daily drafts",
      muted(data.auto_publish
        ? "Three carousel drafts for @instafoodielove are ready by 12:00 London time. Drafts that pass review post automatically at their publish time, at least an hour after they're made. Reject one before then to stop it; approve a weaker one to post it."
        : "Three carousel drafts for @instafoodielove are ready by 12:00 London time. Automatic posting is off, so nothing is posted from here: approve the ones you like, then post them yourself."),
      last ? keyValues([["Last run", when(last.started_at)], ["Result", last.outcome], ["Drafts made", last.drafts],
        ...(last.error ? [["Error", last.error]] : [])])
        : muted(data.state === "not_configured" ? "Not connected: there is no Instagram database on this server yet." : "No daily run recorded yet.")));
    for (const draft of data.drafts) view.append(draftCard(draft, canDecide, data.auto_publish));
  }


  const VIEWS = { overview, "my-day": myDay, energy, agents: agentsView, integrations, automations: automationsView, settings, instagram };

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
    await (VIEWS[section[0]] ? VIEWS[section[0]](view) : placeholder(view, section[0]));
    clearTimeout(loading);
    if (current !== renderCount) return;
    const editing = document.activeElement && $("view").contains(document.activeElement) && document.activeElement.matches("input, textarea, select");
    if (background && editing) return;
    const fresh = [...view.childNodes];
    const title = $("view-title").textContent;
    const previous = sectionViews.get(section[0]);
    // Reuse the nodes already built for identical content: images stay loaded and nothing re-flows.
    const same = previous && previous.title === title && previous.status === $("view-status").textContent
      && previous.nodes.length === fresh.length && previous.nodes.every((node, i) => node.isEqualNode(fresh[i]));
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
