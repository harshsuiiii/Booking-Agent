import { Suspense, type ReactNode } from "react";
import AppShell from "@/components/AppShell";

/** The app's pages. There is no login: every visitor uses the demo agency as an Agent. */
export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    // The shell reads the current path, which is only known at request time.
    <Suspense fallback={<div className="min-h-screen bg-zinc-50" aria-busy="true" />}>
      <AppShell>{children}</AppShell>
    </Suspense>
  );
}
