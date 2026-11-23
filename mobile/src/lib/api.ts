const BASE = "https://iramkhan.com/command-centre/api/v1/";
const ORIGIN = "https://iramkhan.com";
const COOKIE_NAME = "__Host-cc_session";
const TOKEN = /^[A-Za-z0-9_-]{43}$/;

type Fetch = (url: string, init: RequestInit) => Promise<Pick<Response, "headers" | "json" | "ok" | "status">>;
type Method = "GET" | "POST" | "DELETE";
type Route = "health" | "session" | "overview" | "notifications" | "assistant";
type SessionData = { authenticated: true; csrf_token: string; expires_at: string; remembered: boolean };
type Options = { method?: Method; token?: string; csrf?: string; body?: object; page?: number; timeout?: number };

export type Session = { token: string; csrf: string; expiresAt: string; remembered: boolean };
export type Attention = { source: string; severity: string; fact: string; href?: string };
export type Overview = {
  generated_at: string;
  attention: Attention[];
};
export type Inbox = {
  state: string;
  entries: { id: number; created_at: string; source: string; outcome: string; title: string; link: string | null }[];
  page: number;
  pages: number;
};
export type Answer = { answer: string };

export class ApiError extends Error {
  constructor(status: number, code: string) {
    super(code);
    this.status = status;
    this.code = code;
  }
  readonly status: number;
  readonly code: string;
}

export function problem(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.code) {
      case "invalid_credentials": return "That passphrase didn’t match.";
      case "access_check": return "The website returned a browser security check instead of the Jarvis API. Try again later or explore the demo.";
      case "network_error": return "Couldn’t reach Command Centre. Check your connection and try again.";
      case "timeout": return "Command Centre took too long to respond. Try again.";
      case "rate_limited": return "Too many requests. Wait a few minutes and try again.";
      case "authentication_required": return "Your session ended. Sign in again.";
      case "assistant_unavailable": return "Jarvis couldn’t answer right now. Try again later.";
      case "assistant_not_configured": return "The Command Centre assistant is not configured.";
      case "session_unavailable": return "Command Centre didn’t return a usable session.";
      case "secure_storage_unavailable": return "The phone couldn’t securely save your sign-in. Try again.";
    }
  }
  return "Something went wrong. Try again.";
}

function sessionToken(header: string | null) {
  return header?.match(/(?:^|,\s*)__Host-cc_session=([A-Za-z0-9_-]{43})(?=;|$)/)?.[1];
}

function session(data: SessionData, token?: string): Session {
  if (!token || !TOKEN.test(token) || data?.authenticated !== true || !TOKEN.test(data.csrf_token) ||
      !Number.isFinite(Date.parse(data.expires_at)) || typeof data.remembered !== "boolean") {
    throw new ApiError(0, "session_unavailable");
  }
  return { token, csrf: data.csrf_token, expiresAt: data.expires_at, remembered: data.remembered };
}

export function makeApi(fetch: Fetch) {
  async function request<T>(route: Route, options: Options = {}) {
    const { method = "GET", token, csrf, body, page, timeout = 15000 } = options;
    const url = BASE + route + (page === undefined ? "" : `?page=${page}`);
    const headers: Record<string, string> = { Accept: "application/json" };
    if (token && TOKEN.test(token)) headers.Cookie = `${COOKIE_NAME}=${token}`;
    if (method !== "GET") headers.Origin = ORIGIN;
    if (csrf) headers["X-CSRF-Token"] = csrf;
    if (body) headers["Content-Type"] = "application/json";
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url, {
        method, headers, body: body ? JSON.stringify(body) : undefined,
        credentials: "omit", redirect: "error", signal: controller.signal,
      });
      const contentType = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
      if (contentType === "text/html") throw new ApiError(response.status, "access_check");
      if (contentType !== "application/json") throw new ApiError(response.status, "unexpected_response");
      const data = await response.json().catch(() => { throw new ApiError(response.status, "unexpected_response"); });
      if (!response.ok) {
        throw new ApiError(response.status, typeof data?.error === "string" ? data.error : "unexpected_response");
      }
      return { data: data as T, headers: response.headers };
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(0, controller.signal.aborted ? "timeout" : "network_error");
    } finally {
      clearTimeout(timer);
    }
  }

  return {
    async signIn(password: string, remember: boolean) {
      const health = await request<{ status: string }>("health");
      if (health.data?.status !== "ok") throw new ApiError(0, "unexpected_response");
      const { data, headers } = await request<SessionData>("session", { method: "POST", body: { password, remember } });
      return session(data, sessionToken(headers.get("set-cookie")));
    },
    async restore(token: string) {
      return session((await request<SessionData>("session", { token })).data, token);
    },
    async signOut(auth: Session) {
      await request("session", { method: "DELETE", token: auth.token, csrf: auth.csrf });
    },
    async overview(token: string) {
      return (await request<Overview>("overview", { token })).data;
    },
    async inbox(token: string, page: number) {
      if (!Number.isSafeInteger(page) || page < 1 || page > 100000) throw new RangeError("Invalid page");
      return (await request<Inbox>("notifications", { token, page })).data;
    },
    async assistantAvailable(token: string) {
      return (await request<{ available: boolean }>("assistant", { token })).data.available === true;
    },
    async ask(auth: Session, question: string) {
      return (await request<Answer>("assistant", {
        method: "POST", token: auth.token, csrf: auth.csrf, body: { question }, timeout: 65000,
      })).data;
    },
  };
}
