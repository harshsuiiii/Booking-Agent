/**
 * No-login entry, role-specific dashboard/bookings, filters and navigation.
 * API mocked with page.route(); role differences come from the payloads the API sends.
 */
import { test, expect, type Page } from "@playwright/test";
import { json, type Role } from "./helpers";

function bookingItem(role: Role, n: number, state = "TICKETED") {
  const price = role === "admin"
    ? { sell_inr: "10199", currency: "INR", net_inr: "9615", markup_inr: "584" }
    : { sell_inr: "10199", currency: "INR" };
  return {
    booking_id: `b${n}`, reference: `BA-TEST0${n}`, passenger: "A. Sharma", origin: "DEL",
    destination: "BOM", travel_date: "2026-11-20", supplier_id: "sim_gds", state, pnr: "SIMABCDE",
    is_demo: true, created_at: "2026-10-10T06:00:00Z", price,
  };
}

function summary(role: Role) {
  return {
    as_of: "2026-10-18T09:00:00Z", month: "October 2026", bookings_this_month: 12,
    sales_this_month_inr: "171994.24", upcoming_trips: 1,
    next_trips: [{ booking_id: "b1", reference: "BA-TEST01", passenger: "A. Sharma", origin: "DEL",
                   destination: "BOM", travel_date: "2026-11-20", is_demo: true }],
    status_counts: { TICKETED: 1 },
    recent_searches: [{ origin: "BLR", destination: "DXB", departure_date: "2026-12-20", passengers: 1,
                        cabin: "ECONOMY", source: "search", offer_count: 24, searched_at: "2026-10-18T08:00:00Z" }],
    ...(role === "admin" ? { markup_this_month_inr: "7763.24" } : {}),
  };
}

function charts(role: Role) {
  const perf = { net_inr: "1", markup_inr: "1", searches: 5, wins: 3, win_rate: 0.6, error_rate: 0, avg_latency_ms: 800 };
  return {
    days: 30,
    bookings_over_time: Array.from({ length: 30 }, (_, i) => ({
      date: `2026-09-${String((i % 28) + 1).padStart(2, "0")}`, bookings: i % 4, sales_inr: "0",
    })),
    by_supplier: [{ supplier_id: "sim_gds", bookings: 11, sales_inr: "130000",
                    ...(role === "admin" ? { supplier_performance: perf } : {}) }],
    metrics_since: role === "admin" ? "API start" : null,
  };
}

async function mockApp(page: Page, role: Role) {
  const bookingQueries: string[] = [];
  await page.route("**/v1/dashboard/summary", (r) => r.fulfill(json(summary(role))));
  await page.route("**/v1/dashboard/charts", (r) => r.fulfill(json(charts(role))));
  await page.route("**/v1/bookings?**", (r) => {
    const url = new URL(r.request().url());
    bookingQueries.push(url.search);
    const status = url.searchParams.get("status");
    const items = status ? [bookingItem(role, 2, status)] : [bookingItem(role, 1), bookingItem(role, 2, "CANCELLED")];
    return r.fulfill(json({ items, total: items.length, page: 1, page_size: 20 }));
  });
  return bookingQueries;
}

test.describe("No login", () => {
  test("the app opens straight on the dashboard, with no sign-in anywhere", async ({ page }) => {
    await mockApp(page, "agent");
    await page.goto("/");
    await expect(page).toHaveURL(/\/dashboard$/);
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await expect(page.getByText("Bookings this month")).toBeVisible();
    await expect(page.getByRole("button", { name: /sign out/i })).toHaveCount(0);
    await page.goto("/bookings");
    await expect(page.getByRole("heading", { name: "Bookings" })).toBeVisible();
  });

  test("the old login page is gone", async ({ page }) => {
    const res = await page.goto("/login");
    expect(res?.status()).toBe(404);
  });
});

test.describe("Role-specific views", () => {
  test("agent dashboard shows sell prices only", async ({ page }) => {
    await mockApp(page, "agent");
    await page.goto("/dashboard");
    await expect(page.getByText("Bookings this month")).toBeVisible();
    await expect(page.getByText("₹1,71,994")).toBeVisible();
    await expect(page.getByText("Markup earned")).toHaveCount(0);
    await expect(page.getByText("Supplier win rate")).toHaveCount(0);
    await page.goto("/bookings");
    const headers = page.locator("thead th");
    await expect(headers.filter({ hasText: "Sell" })).toHaveCount(1);
    await expect(headers.filter({ hasText: "Net" })).toHaveCount(0);
    await expect(headers.filter({ hasText: "Markup" })).toHaveCount(0);
  });

  test("admin dashboard adds markup, net and supplier win rate", async ({ page }) => {
    await mockApp(page, "admin");
    await page.goto("/dashboard");
    await expect(page.getByText("Markup earned")).toBeVisible();
    await expect(page.getByText("₹7,763")).toBeVisible();
    await expect(page.getByText("Supplier win rate")).toBeVisible();
    await expect(page.getByText("60%")).toBeVisible();
    await page.goto("/bookings");
    await expect(page.locator("thead th").filter({ hasText: "Net" })).toHaveCount(1);
    await expect(page.locator("thead th").filter({ hasText: "Markup" })).toHaveCount(1);
  });
});

test.describe("Bookings list", () => {
  test("filters send status, search text and dates to the API", async ({ page }) => {
    const queries = await mockApp(page, "agent");
    await page.goto("/bookings");
    await expect(page.getByText("BA-TEST01")).toBeVisible();
    await expect(page.getByText("demo data").first()).toBeVisible();

    await page.getByLabel("Status").selectOption("TICKETED");
    await expect.poll(() => queries.at(-1)).toContain("status=TICKETED");
    await page.getByLabel("Search").fill("sharma");
    await expect.poll(() => queries.at(-1)).toContain("q=sharma");
    await page.getByLabel("Travel from").fill("2026-11-01");
    await expect.poll(() => queries.at(-1)).toContain("from=2026-11-01");

    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect.poll(() => queries.at(-1)).not.toContain("status=");
  });
});

test.describe("Navigation", () => {
  test("sidebar collapses to an icon rail and can be restored", async ({ page }) => {
    await mockApp(page, "agent");
    await page.goto("/dashboard");
    await page.getByRole("button", { name: "Collapse sidebar" }).click();
    await expect(page.getByRole("button", { name: "Expand sidebar" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Bookings" })).toBeVisible(); // icon with label
    await page.reload();
    await expect(page.getByRole("button", { name: "Expand sidebar" })).toBeVisible(); // remembered
    await page.getByRole("button", { name: "Expand sidebar" }).click();
    await expect(page.getByRole("button", { name: "Collapse sidebar" })).toBeVisible();
  });

  test("phone: drawer opens, navigates, and closes with Escape", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await mockApp(page, "agent");
    await page.goto("/dashboard");
    await expect(page.getByText("Bookings this month")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBe(0);
    await page.getByRole("button", { name: "Open navigation" }).click();
    const drawer = page.getByRole("dialog", { name: "Navigation" });
    await expect(drawer).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);
    await page.getByRole("button", { name: "Open navigation" }).click();
    await drawer.getByRole("link", { name: "Bookings" }).click();
    await expect(page).toHaveURL(/\/bookings$/);
    await expect(drawer).toHaveCount(0);
  });
});
