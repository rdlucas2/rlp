import { useEffect, useState } from "react";

import { Unauthorized } from "./api";

export type Load<T> = { state: "loading" } | { state: "ready"; data: T } | { state: "error"; message: string };

/**
 * Run `fetcher` and track its result; a new `fetcher` (memoize it with
 * useCallback) runs again. A 401 calls `onUnauthorized`, which sends the user
 * back to the token prompt, instead of showing an error.
 */
export function useLoad<T>(fetcher: () => Promise<T>, onUnauthorized: () => void): Load<T> {
  const [result, setResult] = useState<{ source: () => Promise<T>; load: Load<T> } | null>(null);

  useEffect(() => {
    let live = true;
    fetcher().then(
      (data) => {
        if (live) setResult({ source: fetcher, load: { state: "ready", data } });
      },
      (err: unknown) => {
        if (!live) return;
        if (err instanceof Unauthorized) {
          onUnauthorized();
          return;
        }
        const message = err instanceof Error ? err.message : String(err);
        setResult({ source: fetcher, load: { state: "error", message } });
      },
    );
    return () => {
      live = false;
    };
  }, [fetcher, onUnauthorized]);

  return result !== null && result.source === fetcher ? result.load : { state: "loading" };
}
