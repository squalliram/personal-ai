import { fetch } from "expo/fetch";
import * as SecureStore from "expo-secure-store";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, makeApi, type Session } from "./api";

const KEY = "jarvis-command-session-v1";
const accessibility = { keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY };
export const api = makeApi(fetch);
type Access = { kind: "signed-out" } | { kind: "demo" } | { kind: "live"; session: Session };
type Auth = {
  access: Access;
  loading: boolean;
  notice: string | null;
  signIn: (password: string, remember: boolean) => Promise<void>;
  signOut: () => Promise<void>;
  startDemo: () => void;
  invalidate: (token: string) => void;
};
const AuthContext = createContext<Auth | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [access, setAccess] = useState<Access>({ kind: "signed-out" });
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);
  const current = useRef<Session | null>(null);

  useEffect(() => {
    let active = true;
    async function restore() {
      try {
        const token = await SecureStore.getItemAsync(KEY);
        if (!active || !token) return;
        const session = await api.restore(token);
        if (active) {
          current.current = session;
          setAccess({ kind: "live", session });
        }
      } catch (error) {
        if (!active) return;
        if (error instanceof ApiError && error.status === 401) {
          await SecureStore.deleteItemAsync(KEY).catch(() => {});
          if (active) setNotice("Your saved session ended. Sign in again.");
        } else {
          setNotice("Couldn’t restore your saved session. Sign in when Command Centre is reachable.");
        }
      } finally {
        if (active) setLoading(false);
      }
    }
    void restore();
    return () => { active = false; };
  }, []);

  const signIn = useCallback(async (password: string, remember: boolean) => {
    setNotice(null);
    const session = await api.signIn(password, remember);
    try {
      if (session.remembered) await SecureStore.setItemAsync(KEY, session.token, accessibility);
      else await SecureStore.deleteItemAsync(KEY);
    } catch {
      await api.signOut(session).catch(() => {});
      throw new ApiError(0, "secure_storage_unavailable");
    }
    current.current = session;
    setAccess({ kind: "live", session });
  }, []);

  const signOut = useCallback(async () => {
    const session = current.current;
    current.current = null;
    let revoked = true;
    let cleared = true;
    try {
      if (session) await SecureStore.deleteItemAsync(KEY);
    } catch {
      cleared = false;
    }
    try {
      if (session) await api.signOut(session);
    } catch (error) {
      revoked = error instanceof ApiError && error.status === 401;
    }
    setNotice(!revoked
      ? cleared
        ? "Signed out here. The server couldn’t confirm; you can revoke this device from website Settings."
        : "Couldn’t clear the saved session or confirm sign-out. Revoke this device from website Settings."
      : null);
    setAccess({ kind: "signed-out" });
  }, []);

  const invalidate = useCallback((token: string) => {
    if (current.current?.token !== token) return;
    current.current = null;
    void SecureStore.deleteItemAsync(KEY).catch(() => {});
    setNotice("Your session ended. Sign in again.");
    setAccess({ kind: "signed-out" });
  }, []);

  const startDemo = useCallback(() => {
    setNotice(null);
    setAccess({ kind: "demo" });
  }, []);

  const value = useMemo(() => ({ access, loading, notice, signIn, signOut, invalidate, startDemo }),
    [access, loading, notice, signIn, signOut, invalidate, startDemo]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("AuthProvider is missing");
  return auth;
}
