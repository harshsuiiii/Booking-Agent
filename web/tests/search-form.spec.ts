/**
 * The search form: journey types, dates (DD / MM / YYYY) with time of day, passengers,
 * class, advanced options, validation and URL prefill. The API is mocked; each test
 * checks the exact body the form posts to /v1/search.
 */

import { test, expect, type Page } from "@playwright/test";
import { signIn } from "./helpers";

const SEARCH_ID = "form-search-1";

type Body = {
  journey_type: string;
  legs: { origin: string; destination: string; departure_date: string; time_window: string }[];
  pax_counts: { adults: number; children: number; infants: number };
  cabin: string;
  filters: Record<string, unknown>;
};

const seg = (o: string, d: string, dep: string, arr: string) => ({
  marketing_carrier: { iata_code: "AI", name: "Air India" },
  operating_carrier: null,
  flight_number: "101",
  origin: o,
  destination: d,
  departure_at: dep,
  arrival_at: arr,
  cabin_class: "ECONOMY",
  booking_class: "Y",
  duration_minutes: 120,
  stops: 0,
});

const MULTI_OFFER = {
  offer_id: "multi-1",
  supplier_id: "mock_supplier",
  supplier_offer_ref: "REF-M",
  slices: [
    { segments: [seg("DEL", "BOM", "2030-01-10T06:00:00+05:30", "2030-01-10T08:00:00+05:30")] },
    { segments: [seg("BOM", "DXB", "2030-01-12T10:00:00+05:30", "2030-01-12T12:00:00+04:00")] },
    { segments: [seg("DXB", "DEL", "2030-01-15T20:00:00+04:00", "2030-01-16T01:30:00+05:30")] },
  ],
  fare_basis: "Y",
  fare_family: "SAVER",
  cabin_class: "ECONOMY",
  baggage: { cabin_pieces: 1, cabin_kg: "7", checked_pieces: 1, checked_kg: "15" },
  fare_rules: {
    change: { allowed: true, fee: null },
    cancel: { allowed: true, fee: null },
    refund: { allowed: true, fee: null },
  },
  price: { currency: "INR", sell_total: "52000", taxes_total: "4000" },
  sell_inr: "52000",
  expires_at: "2030-01-01T01:00:00+00:00",
  pax_count: 1,
};

/** Mock the search API; returns the bodies the form posted. */
async function mockSearch(page: Page, groups: unknown[] = []): Promise<Body[]> {
  const bodies: Body[] = [];
  await page.route("**/v1/search", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    bodies.push(route.request().postDataJSON() as Body);
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ search_id: SEARCH_ID }) });
  });
  await page.route(`**/v1/search/${SEARCH_ID}/stream`, async (route) => {
    const done = {
      type: "search_complete",
      groups,
      suppliers_succeeded: ["mock_supplier"],
      suppliers_failed: [],
      suppliers_timed_out: [],
      suppliers_skipped: [],
      elapsed_seconds: 0.2,
    };
    await route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
      body: `data: ${JSON.stringify(done)}\n\ndata: [DONE]\n\n`,
    });
  });
  return bodies;
}

async function open(page: Page, query = "") {
  await signIn(page);
  await page.goto(`/search${query}`);
  // Hydrated once the first date is filled in (dates default on the client only).
  await expect(page.getByLabel(/: day$/).first()).not.toHaveValue("", { timeout: 30_000 });
}

async function typeDate(page: Page, label: string, d: string, m: string, y: string) {
  await page.getByLabel(`${label}: day`).fill(d);
  await page.getByLabel(`${label}: month`).fill(m);
  await page.getByLabel(`${label}: year`).fill(y);
}

const submit = (page: Page) => page.getByRole("button", { name: "Find available flights" }).click();

test.describe("Search form", () => {
  test("defaults to a return trip and posts both legs with their time of day", async ({ page }) => {
    const bodies = await mockSearch(page);
    await open(page);
    await expect(page.getByRole("radio", { name: "Return" })).toBeChecked();

    await typeDate(page, "Departure date", "10", "01", "2030");
    await typeDate(page, "Return date", "17", "01", "2030");
    await page.getByLabel("Departure time").selectOption("morning");
    await page.getByLabel("Return time").selectOption("evening");
    await page.getByLabel("Class").selectOption("BUSINESS");
    await submit(page);

    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0]).toMatchObject({
      journey_type: "return",
      cabin: "BUSINESS",
      legs: [
        { origin: "DEL", destination: "BOM", departure_date: "2030-01-10", time_window: "morning" },
        { origin: "BOM", destination: "DEL", departure_date: "2030-01-17", time_window: "evening" },
      ],
    });
    // The URL now holds the search, so a reload or a shared link refills the form.
    await expect(page).toHaveURL(/trip=return/);
    await expect(page).toHaveURL(/legs=DEL\.BOM\.2030-01-10\.morning/);
  });

  test("one way hides the return date and posts one leg", async ({ page }) => {
    const bodies = await mockSearch(page);
    await open(page);
    await page.getByRole("radio", { name: "One way" }).check();
    await expect(page.getByText("Return date")).toHaveCount(0);
    await page.getByRole("button", { name: "Swap origin and destination" }).click();
    await submit(page);

    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0].journey_type).toBe("one_way");
    expect(bodies[0].legs).toHaveLength(1);
    expect(bodies[0].legs[0]).toMatchObject({ origin: "BOM", destination: "DEL" });
  });

  test("multi-city posts every flight and the card shows every slice", async ({ page }) => {
    const bodies = await mockSearch(page, [{ dedupe_key: "m", primary: MULTI_OFFER, alternates: [] }]);
    await open(page);
    await page.getByRole("radio", { name: "Multi-city" }).check();
    await expect(page.getByTestId("multi-city-leg")).toHaveCount(2);
    // Flight 2 starts where flight 1 lands.
    await expect(page.getByRole("combobox", { name: "Origin (flight 2)" })).toHaveValue("BOM");

    await page.getByRole("combobox", { name: "Destination (flight 2)" }).fill("DXB");
    await page.getByRole("button", { name: "+ Add another flight" }).click();
    await expect(page.getByRole("combobox", { name: "Origin (flight 3)" })).toHaveValue("DXB");
    await page.getByRole("combobox", { name: "Destination (flight 3)" }).fill("DEL");
    await typeDate(page, "Date (flight 1)", "10", "01", "2030");
    await typeDate(page, "Date (flight 2)", "12", "01", "2030");
    await typeDate(page, "Date (flight 3)", "15", "01", "2030");
    await page.getByLabel("Time (flight 3)").selectOption("evening");
    await submit(page);

    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0].journey_type).toBe("multi_city");
    expect(bodies[0].legs.map((l) => `${l.origin}-${l.destination}-${l.departure_date}-${l.time_window}`)).toEqual([
      "DEL-BOM-2030-01-10-any",
      "BOM-DXB-2030-01-12-any",
      "DXB-DEL-2030-01-15-evening",
    ]);

    const card = page.getByTestId("offer-card");
    await expect(card.getByTestId("offer-slice")).toHaveCount(3);
    await expect(card).toContainText("Flight 3");
    await expect(card).toContainText("20:00 → 01:30"); // airport-local times
  });

  test("a flight can be removed once there are more than two", async ({ page }) => {
    await mockSearch(page);
    await open(page);
    await page.getByRole("radio", { name: "Multi-city" }).check();
    await expect(page.getByRole("button", { name: /Remove/ })).toHaveCount(0);
    await page.getByRole("button", { name: "+ Add another flight" }).click();
    await page.getByRole("button", { name: "Remove flight 3" }).click();
    await expect(page.getByTestId("multi-city-leg")).toHaveCount(2);
  });

  test("passengers: children and infants, limits enforced", async ({ page }) => {
    const bodies = await mockSearch(page);
    await open(page);
    await page.getByRole("button", { name: "1 adult" }).click();

    // No more infants than adults.
    await page.getByRole("button", { name: "More infants" }).click();
    await expect(page.getByRole("button", { name: "More infants" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Fewer adults" })).toBeDisabled();

    await page.getByRole("button", { name: "More adults" }).click();
    for (let i = 0; i < 6; i++) await page.getByRole("button", { name: "More children" }).click();
    // 2 + 6 + 1 = 9: the cap.
    await expect(page.getByRole("button", { name: "More children" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "More adults" })).toBeDisabled();
    await page.getByRole("button", { name: "Done" }).click();
    await expect(page.getByRole("button", { name: "2 adults, 6 children, 1 infant" })).toBeVisible();

    await submit(page);
    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0].pax_counts).toEqual({ adults: 2, children: 6, infants: 1 });
  });

  test("advanced options are sent as filters", async ({ page }) => {
    const bodies = await mockSearch(page);
    await open(page);
    await page.getByRole("button", { name: "Advanced options" }).click();
    await page.getByLabel("Stops (each flight)").selectOption("0");
    await page.getByLabel("Preferred airlines").fill("ai, 6e");
    await page.getByLabel("Checked bag included").check();
    await page.getByLabel("No overnight layovers").check();
    await expect(page.getByRole("button", { name: "Advanced options (4)" })).toBeVisible();
    await submit(page);

    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0].filters).toEqual({
      max_stops: 0,
      preferred_airlines: ["AI", "6E"],
      bags_included: true,
      refundable_only: false,
      no_overnight_layover: true,
    });
  });

  test("an impossible or out-of-order date is caught before searching", async ({ page }) => {
    const bodies = await mockSearch(page);
    await open(page);
    await typeDate(page, "Departure date", "31", "02", "2030");
    await submit(page);
    await expect(page.locator("ul[role=alert]")).toContainText("Departure date: enter a real date");

    await typeDate(page, "Departure date", "20", "01", "2030");
    await typeDate(page, "Return date", "10", "01", "2030");
    await submit(page);
    await expect(page.locator("ul[role=alert]")).toContainText("Return date is before the previous flight");
    expect(bodies).toHaveLength(0);
  });

  test("the URL prefills a multi-city search", async ({ page }) => {
    await mockSearch(page);
    await open(page, "?trip=multi_city&legs=DEL.BOM.2030-01-10.morning~BOM.GOI.2030-01-12.any&adt=2&inf=1&cabin=BUSINESS&noovn=1");
    await expect(page.getByRole("radio", { name: "Multi-city" })).toBeChecked();
    await expect(page.getByRole("combobox", { name: "Destination (flight 2)" })).toHaveValue("GOI");
    await expect(page.getByLabel("Date (flight 2): day")).toHaveValue("12");
    await expect(page.getByLabel("Time (flight 1)")).toHaveValue("morning");
    await expect(page.getByRole("button", { name: "2 adults, 1 infant" })).toBeVisible();
    await expect(page.getByLabel("Class")).toHaveValue("BUSINESS");
    await expect(page.getByRole("button", { name: "Advanced options (1)" })).toBeVisible();
  });

  test("the older from/to/date link still prefills", async ({ page }) => {
    await mockSearch(page);
    await open(page, "?from=BLR&to=GOI&date=2030-03-05");
    await expect(page.getByRole("radio", { name: "One way" })).toBeChecked();
    await expect(page.getByRole("combobox", { name: "Origin", exact: true })).toHaveValue("BLR");
    await expect(page.getByLabel("Departure date: month")).toHaveValue("03");
  });
});
