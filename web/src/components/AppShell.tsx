"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, useSyncExternalStore, type ComponentType, type ReactNode } from "react";
import {
  ChatIcon,
  CloseIcon,
  CollapseIcon,
  DashboardIcon,
  ExpandIcon,
  MenuIcon,
  PlaneIcon,
  SearchIcon,
  TicketIcon,
} from "@/components/Icons";

const NAV: { href: string; label: string; icon: ComponentType<{ size?: number }> }[] = [
  { href: "/dashboard", label: "Dashboard", icon: DashboardIcon },
  { href: "/search", label: "Search", icon: SearchIcon },
  { href: "/bookings", label: "Bookings", icon: TicketIcon },
  { href: "/assistant", label: "Assistant", icon: ChatIcon },
];

// Sidebar collapsed state, remembered per browser (a convenience; safe to lose).
const KEY = "ba.sidebar.collapsed";
const listeners = new Set<() => void>();
function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}
function writeCollapsed(value: boolean) {
  try {
    window.localStorage.setItem(KEY, value ? "1" : "0");
  } catch {
    /* storage blocked: keep the in-memory choice */
  }
  listeners.forEach((l) => l());
}
function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function DemoBadge({ compact = false }: { compact?: boolean }) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border border-violet-200 bg-violet-50 px-2 py-0.5 text-[11px] font-medium text-violet-700"
      title="Demo environment: simulated suppliers and seeded bookings. No real fares or tickets."
    >
      <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-violet-500" />
      {compact ? "Demo" : "Demo environment · simulated data"}
    </span>
  );
}

function Brand({ collapsed }: { collapsed: boolean }) {
  return (
    <Link
      href="/dashboard"
      className="flex items-center gap-2 rounded-lg px-1 py-1 focus-visible:outline-2 focus-visible:outline-blue-600"
    >
      <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-blue-600 text-white">
        <PlaneIcon size={18} />
      </span>
      {!collapsed && <span className="text-sm font-semibold tracking-tight text-zinc-900">Booking Agent</span>}
    </Link>
  );
}

function NavLinks({ collapsed, onNavigate }: { collapsed: boolean; onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <ul className="flex flex-col gap-1">
      {NAV.map(({ href, label, icon: Icon }) => {
        const active = pathname === href || pathname.startsWith(`${href}/`);
        return (
          <li key={href}>
            <Link
              href={href}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              title={collapsed ? label : undefined}
              className={`flex items-center gap-3 rounded-lg px-2.5 py-2 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-blue-600 ${
                active ? "bg-blue-50 text-blue-700" : "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900"
              } ${collapsed ? "justify-center" : ""}`}
            >
              <Icon size={20} />
              {collapsed ? <span className="sr-only">{label}</span> : label}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

export default function AppShell({ children }: { children: ReactNode }) {
  const collapsed = useSyncExternalStore(subscribe, readCollapsed, () => false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const pathname = usePathname();

  // Close the phone drawer on navigation and with Escape.
  const [seenPath, setSeenPath] = useState(pathname);
  if (pathname !== seenPath) {
    setSeenPath(pathname);
    setDrawerOpen(false);
  }
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDrawerOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  return (
    <div className="flex min-h-screen bg-zinc-50">
      <a
        href="#main"
        className="sr-only z-50 rounded bg-white px-3 py-2 text-sm font-medium focus:not-sr-only focus:fixed focus:left-3 focus:top-3"
      >
        Skip to content
      </a>

      {/* Desktop sidebar: full width or icon rail; the toggle always stays visible. */}
      <aside
        aria-label="Main navigation"
        className={`sticky top-0 hidden h-screen shrink-0 flex-col gap-4 border-r border-zinc-200 bg-white p-3 md:flex ${
          collapsed ? "w-16" : "w-60"
        } transition-[width] duration-200`}
      >
        <Brand collapsed={collapsed} />
        {!collapsed && <DemoBadge />}
        <nav className="flex-1">
          <NavLinks collapsed={collapsed} />
        </nav>
        <button
          type="button"
          onClick={() => writeCollapsed(!collapsed)}
          aria-expanded={!collapsed}
          className={`flex items-center gap-3 rounded-lg px-2.5 py-2 text-sm text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900 focus-visible:outline-2 focus-visible:outline-blue-600 ${
            collapsed ? "justify-center" : ""
          }`}
        >
          {collapsed ? <ExpandIcon size={20} /> : <CollapseIcon size={20} />}
          {collapsed ? <span className="sr-only">Expand sidebar</span> : "Collapse sidebar"}
        </button>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Phone top bar */}
        <header className="sticky top-0 z-30 flex items-center justify-between gap-2 border-b border-zinc-200 bg-white/95 px-4 py-2 backdrop-blur md:hidden">
          <button
            type="button"
            onClick={() => setDrawerOpen(true)}
            aria-expanded={drawerOpen}
            aria-controls="mobile-nav"
            className="rounded-lg p-2 text-zinc-700 hover:bg-zinc-100 focus-visible:outline-2 focus-visible:outline-blue-600"
          >
            <MenuIcon />
            <span className="sr-only">Open navigation</span>
          </button>
          <Brand collapsed={false} />
          <DemoBadge compact />
        </header>

        {drawerOpen && (
          <div className="fixed inset-0 z-40 md:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
            <button
              type="button"
              aria-label="Close navigation"
              className="absolute inset-0 bg-black/30"
              onClick={() => setDrawerOpen(false)}
            />
            <div id="mobile-nav" className="absolute inset-y-0 left-0 flex w-72 max-w-[85%] flex-col gap-4 bg-white p-4 shadow-xl">
              <div className="flex items-center justify-between">
                <Brand collapsed={false} />
                <button
                  type="button"
                  onClick={() => setDrawerOpen(false)}
                  className="rounded-lg p-2 text-zinc-500 hover:bg-zinc-100 focus-visible:outline-2 focus-visible:outline-blue-600"
                >
                  <CloseIcon />
                  <span className="sr-only">Close navigation</span>
                </button>
              </div>
              <DemoBadge />
              <nav className="flex-1">
                <NavLinks collapsed={false} onNavigate={() => setDrawerOpen(false)} />
              </nav>
            </div>
          </div>
        )}

        <main id="main" className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 md:px-8">
          {children}
        </main>
      </div>
    </div>
  );
}
