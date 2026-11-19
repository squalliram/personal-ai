import { useCallback, useEffect, useState } from "react";
import { ApiError, problem } from "./api";
import { useAuth } from "./auth";

export function useResource<T>(fetchData: (token: string) => Promise<T>, sample: T) {
  const { access, invalidate } = useAuth();
  const token = access.kind === "live" ? access.session.token : undefined;
  const [revision, setRevision] = useState(0);
  const [result, setResult] = useState<{ revision: number; data?: T; error?: string }>();
  const reload = useCallback(() => setRevision((value) => value + 1), []);

  useEffect(() => {
    if (!token) return;
    let active = true;
    void fetchData(token).then((data) => {
      if (active) setResult({ revision, data });
    }, (error) => {
      if (!active) return;
      if (error instanceof ApiError && error.status === 401) invalidate(token);
      else setResult({ revision, error: problem(error) });
    });
    return () => { active = false; };
  }, [token, fetchData, invalidate, revision]);

  return {
    data: access.kind === "demo" ? sample : result?.data,
    error: access.kind === "live" && result?.revision === revision ? result.error : undefined,
    loading: access.kind === "live" && result?.revision !== revision,
    reload,
  };
}
