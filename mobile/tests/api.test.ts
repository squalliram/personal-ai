import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, makeApi } from "../src/lib/api.ts";

const token = "a".repeat(43);
const csrf = "b".repeat(43);
const live = { authenticated: true, csrf_token: csrf, expires_at: "2026-11-19T18:00:00Z", remembered: true };
const json = (data: unknown, status = 200, cookie?: string) => Response.json(data, {
  status, headers: cookie ? { "Set-Cookie": cookie } : undefined,
});
const replies = (...list: Response[]) => {
  const calls: { url: string; init: RequestInit }[] = [];
  return { calls, api: makeApi(async (url, init) => {
    calls.push({ url, init });
    const response = list.shift();
    assert.ok(response);
    return response;
  }) };
};

test("preflights login, uses only the expected HTTPS session cookie, and suppresses redirects and the shared cookie jar", async () => {
  const { api, calls } = replies(
    json({ status: "ok" }),
    json(live, 200, `edge=123; Secure, __Host-cc_session=${token}; Path=/; Secure; HttpOnly; SameSite=Strict`),
    json(live),
    json({ authenticated: false }),
  );
  const auth = await api.signIn("mock passphrase", true);
  assert.deepEqual(auth, { token, csrf, expiresAt: live.expires_at, remembered: true });
  assert.deepEqual(await api.restore(token), auth);
  await api.signOut(auth);
  assert.equal(calls[0].url, "https://iramkhan.com/command-centre/api/v1/health");
  assert.equal(calls[0].init.body, undefined);
  assert.equal(new Headers(calls[1].init.headers).get("origin"), "https://iramkhan.com");
  assert.equal(calls[1].init.body, JSON.stringify({ password: "mock passphrase", remember: true }));
  assert.equal(new Headers(calls[2].init.headers).get("cookie"), `__Host-cc_session=${token}`);
  assert.equal(calls[3].init.method, "DELETE");
  assert.equal(new Headers(calls[3].init.headers).get("x-csrf-token"), csrf);
  for (const { init } of calls) {
    assert.equal(init.credentials, "omit");
    assert.equal(init.redirect, "error");
  }
});

test("does not submit a passphrase if the health check returns HTML", async () => {
  const challenge = new Response("<html>Security Check Required</html>", { headers: { "Content-Type": "text/html" } });
  const { api, calls } = replies(challenge);
  await assert.rejects(api.signIn("not sent", false), (e) => e instanceof ApiError && e.code === "access_check");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.body, undefined);
});

test("does not accept a foreign cookie or a malformed session", async () => {
  const bad = replies(json({ status: "ok" }), json(live, 200, `not__Host-cc_session=${token}`));
  const expired = replies(json({ ...live, expires_at: "unparseable" }));
  await assert.rejects(bad.api.signIn("mock passphrase", false), { code: "session_unavailable" });
  await assert.rejects(expired.api.restore(token), { code: "session_unavailable" });
});

test("preserves authentication failures and sends CSRF only for assistant operations", async () => {
  const { api, calls } = replies(json({ error: "authentication_required" }, 401), json({ answer: "Example" }));
  await assert.rejects(api.inbox(token, 2), { status: 401, code: "authentication_required" });
  assert.match(calls[0].url, /notifications\?page=2$/);
  assert.equal(new Headers(calls[0].init.headers).get("x-csrf-token"), null);
  const answer = await api.ask({ token, csrf, expiresAt: live.expires_at, remembered: false }, "What’s new?");
  assert.equal(answer.answer, "Example");
  assert.equal(new Headers(calls[1].init.headers).get("x-csrf-token"), csrf);
  await assert.rejects(api.inbox(token, Infinity), RangeError);
  assert.equal(calls.length, 2);
});
