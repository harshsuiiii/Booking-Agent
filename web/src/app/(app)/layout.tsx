import { Suspense, type ReactNode } from "react";
import AppShell from "@/components/AppShell";
import { AuthProvider } from "@/lib/auth";

/** Every page in this group needs a signed-in user (proxy.ts redirects otherwise). */
export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      {/* The shell reads the current path, which is only known at request time. */}
      <Suspense fallback={<div className="min-h-screen bg-zinc-50" aria-busy="true" />}>
        <AppShell>{children}</AppShell>
      </Suspense>
    </AuthProvider>
  );
}
