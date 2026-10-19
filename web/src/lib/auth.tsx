"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  useSyncExternalStore,
  ReactNode,
} from "react";
import { useRouter } from "next/navigation";
import type { User } from "@/types/dashboard";
import { ApiError, getJson, postJson, setUnauthorizedHandler } from "@/lib/api";

interface AuthState {
  user: User | null;
  loading: boolean;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState>({
  user: null,
  loading: true,
  signOut: async () => {},
});

const noSubscribe = () => () => {};

/**
 * The signed-in user. While a part of the page is still hydrating this reports "not loaded
 * yet", as the server HTML did: /v1/auth/me can answer before a Suspense boundary (the app
 * shell) hydrates, and rendering the user then would not match the server's markup.
 */
export function useAuth(): AuthState {
  const auth = useContext(AuthContext);
  const hydrated = useSyncExternalStore(noSubscribe, () => true, () => false);
  return hydrated ? auth : { ...auth, user: null, loading: true };
}

/**
 * Loads the signed-in user for the protected app and turns any 401 from the API into a
 * friendly redirect to /login (with "your session expired" when that's the cause).
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const toLogin = useCallback(
    (reason: string) => {
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      router.replace(`/login?reason=${reason}&next=${next}`);
    },
    [router],
  );

  useEffect(() => {
    setUnauthorizedHandler((code) => toLogin(code === "session_expired" ? "expired" : "signin"));
    return () => setUnauthorizedHandler(null);
  }, [toLogin]);

  useEffect(() => {
    let cancelled = false;
    getJson<User>("/v1/auth/me")
      .then((u) => {
        if (!cancelled) setUser(u);
      })
      .catch((err) => {
        if (cancelled) return;
        const code = err instanceof ApiError ? err.code : undefined;
        toLogin(code === "session_expired" ? "expired" : "signin");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // Only on first load; later 401s come through the unauthorized handler.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const signOut = useCallback(async () => {
    await postJson("/v1/auth/logout").catch(() => undefined);
    setUser(null);
    router.replace("/login?reason=signed-out");
  }, [router]);

  return (
    <AuthContext.Provider value={{ user, loading, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}
