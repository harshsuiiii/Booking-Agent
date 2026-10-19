"use client";

import { Suspense, useEffect, useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { AuthConfig, User } from "@/types/dashboard";
import { ApiError, getJson, postJson } from "@/lib/api";
import { PlaneIcon } from "@/components/Icons";
import { DemoBadge } from "@/components/AppShell";

const REASONS: Record<string, { tone: "info" | "warn"; text: string }> = {
  expired: { tone: "warn", text: "Your session has expired. Please sign in again." },
  signin: { tone: "info", text: "Please sign in to continue." },
  "signed-out": { tone: "info", text: "You've signed out." },
};

/** Only same-app paths, never an external URL. */
function safeNext(next: string | null): string {
  return next && next.startsWith("/") && !next.startsWith("//") && next !== "/login" ? next : "/dashboard";
}

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const reason = REASONS[params.get("reason") ?? ""];

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState<"password" | "demo" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [configError, setConfigError] = useState(false);

  useEffect(() => {
    getJson<AuthConfig>("/v1/auth/config")
      .then(setConfig)
      .catch(() => setConfigError(true));
  }, []);

  async function signIn(kind: "password" | "demo", e?: FormEvent) {
    e?.preventDefault();
    setBusy(kind);
    setError(null);
    try {
      await (kind === "demo"
        ? postJson<User>("/v1/auth/demo")
        : postJson<User>("/v1/auth/login", { email: email.trim(), password }));
      router.replace(next);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Can't reach the server. Check that the API is running on port 8000.",
      );
      setBusy(null);
    }
  }

  const demoAgent = config?.demo_accounts.find((a) => a.role === "agent");

  return (
    <div className="grid min-h-screen place-items-center bg-gradient-to-b from-blue-50 to-zinc-50 px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <span className="grid h-12 w-12 place-items-center rounded-2xl bg-blue-600 text-white shadow-sm">
            <PlaneIcon size={26} />
          </span>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">Booking Agent</h1>
            <p className="mt-1 text-sm text-zinc-600">Search, compare and book flights for your clients.</p>
          </div>
          <DemoBadge />
        </div>

        <div className="rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm">
          {reason && (
            <p
              role="status"
              className={`mb-4 rounded-lg border px-3 py-2 text-sm ${
                reason.tone === "warn"
                  ? "border-amber-200 bg-amber-50 text-amber-900"
                  : "border-blue-100 bg-blue-50 text-blue-900"
              }`}
            >
              {reason.text}
            </p>
          )}

          <form onSubmit={(e) => void signIn("password", e)} className="flex flex-col gap-4" noValidate>
            <label className="flex flex-col gap-1 text-sm font-medium text-zinc-700">
              Email
              <input
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="rounded-lg border border-zinc-300 px-3 py-2 text-sm font-normal focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
                placeholder="agent@demo.test"
              />
            </label>
            <label className="flex flex-col gap-1 text-sm font-medium text-zinc-700">
              Password
              <input
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="rounded-lg border border-zinc-300 px-3 py-2 text-sm font-normal focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
              />
            </label>

            {error && (
              <p role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
                {error}
              </p>
            )}

            <button
              type="submit"
              disabled={busy !== null || !email || !password}
              className="rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:opacity-50"
            >
              {busy === "password" ? "Signing in…" : "Sign in"}
            </button>
          </form>

          {config?.demo_mode && (
            <>
              <div className="my-5 flex items-center gap-3 text-xs text-zinc-400">
                <span className="h-px flex-1 bg-zinc-200" />
                or
                <span className="h-px flex-1 bg-zinc-200" />
              </div>
              <button
                type="button"
                onClick={() => void signIn("demo")}
                disabled={busy !== null || !demoAgent?.available}
                className="w-full rounded-lg border border-zinc-300 bg-white px-4 py-2.5 text-sm font-semibold text-zinc-800 hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:opacity-50"
              >
                {busy === "demo" ? "Signing in…" : "Continue as demo agent"}
              </button>
            </>
          )}
        </div>

        {config?.demo_mode && config.demo_accounts.length > 0 && (
          <section aria-labelledby="demo-accounts" className="mt-4 rounded-xl border border-zinc-200 bg-white/70 p-4 text-sm">
            <h2 id="demo-accounts" className="font-medium text-zinc-800">Demo accounts</h2>
            <ul className="mt-2 flex flex-col gap-1.5">
              {config.demo_accounts.map((a) => (
                <li key={a.email} className="flex flex-wrap items-center justify-between gap-2">
                  <button
                    type="button"
                    onClick={() => setEmail(a.email)}
                    className="font-mono text-zinc-800 underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-blue-600"
                    title="Use this email"
                  >
                    {a.email}
                  </button>
                  <span className="text-xs text-zinc-500">
                    Role <span className="font-medium capitalize text-zinc-700">{a.role}</span>
                    {!a.available && " · not set up"}
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-zinc-500">
              Passwords are set in the server&apos;s <code>.env</code> (DEMO_AGENT_PASSWORD,
              DEMO_ADMIN_PASSWORD). Data here is simulated; no real fares or tickets.
            </p>
          </section>
        )}
        {configError && (
          <p role="alert" className="mt-4 text-center text-sm text-red-700">
            Can&apos;t reach the API. Start it on port 8000 and reload.
          </p>
        )}
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}
