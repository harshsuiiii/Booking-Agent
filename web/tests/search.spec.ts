/**
 * Playwright tests for the search-to-results flow.
 * The backend API is mocked via page.route() so no real server is needed.
 */

import { test, expect, type Page } from "@playwright/test";

/** Open the search page and wait until it is hydrated (the date is filled client-side). */
async function gotoSearch(page: Page) {
  await page.goto("/search");
  await expect(page.getByLabel("Departure date: day")).not.toHaveValue("", { timeout: 30_000 });
}

const SEARCH_ID = "test-search-id-123";

const MOCK_OFFER = {
  offer_id: "offer-001",
  supplier_id: "mock_supplier",
  supplier_offer_ref: "REF001",
  slices: [
    {
      segments: [
        {
          marketing_carrier: { iata_code: "AI", name: "Air India" },
          operating_carrier: null,
          flight_number: "101",
          origin: "DEL",
          destination: "BOM",
          departure_at: "2025-09-01T06:00:00+05:30",
          arrival_at: "2025-09-01T08:00:00+05:30",
          cabin_class: "ECONOMY",
          booking_class: "Y",
          duration_minutes: 120,
          stops: 0,
        },
      ],
    },
  ],
  fare_basis: "YOWCHIN",
  fare_family: "SAVER",
  cabin_class: "ECONOMY",
  baggage: {
    cabin_pieces: 1,
    cabin_kg: "7",
    checked_pieces: 1,
    checked_kg: "15",
  },
  fare_rules: {
    change: { allowed: true, fee: null },
    cancel: { allowed: "UNKNOWN", fee: "UNKNOWN" },
    refund: { allowed: false, fee: null },
  },
  price: {
    base_fare: "3000",
    taxes: [
      { code: "YQ", description: "Fuel surcharge", amount: "500" },
      { code: "IN", description: "GST", amount: "200" },
    ],
    fuel_surcharge: "0",
    supplier_fee: "0",
    agency_markup: "0",
    currency: "INR",
  },
  total_inr: "3700",
  sell_inr: "3700",
  expires_at: "2025-09-01T01:00:00+00:00",
  pax_count: 1,
};

const MOCK_GROUP = {
  dedupe_key: "abc123",
  primary: MOCK_OFFER,
  alternates: [],
};

type MockGroup = { dedupe_key: string; primary: typeof MOCK_OFFER; alternates: never[] };

/** Serve a completed search stream containing the given groups. */
async function mockStream(page: Page, groups: MockGroup[]) {
  await page.route(`**/v1/search/${SEARCH_ID}/stream`, async (route) => {
    const events = [
      { type: "source_started", supplier_id: "mock_supplier", searched_at: new Date().toISOString() },
      { type: "offers_partial", supplier_id: "mock_supplier", offers_count: groups.length, groups },
      {
        type: "search_complete",
        groups,
        suppliers_succeeded: ["mock_supplier"],
        suppliers_failed: [],
        suppliers_timed_out: [],
        suppliers_skipped: [],
        elapsed_seconds: 0.1,
      },
    ];
    await route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
      body: events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n",
    });
  });
}

const BASE_SEG = MOCK_OFFER.slices[0].segments[0];

/** DEL 17:25 → BOM 18:55, 2h 30m layover, BOM 21:25 → GOI 23:00: 3h 5m in the air, 5h 35m elapsed. */
const ONE_STOP_LONG_LAYOVER = {
  ...MOCK_OFFER,
  offer_id: "one-stop",
  total_inr: "3000",
  sell_inr: "3000",
  slices: [
    {
      segments: [
        {
          ...BASE_SEG,
          destination: "BOM",
          departure_at: "2025-09-01T17:25:00+05:30",
          arrival_at: "2025-09-01T18:55:00+05:30",
          duration_minutes: 90,
        },
        {
          ...BASE_SEG,
          flight_number: "202",
          origin: "BOM",
          destination: "GOI",
          departure_at: "2025-09-01T21:25:00+05:30",
          arrival_at: "2025-09-01T23:00:00+05:30",
          duration_minutes: 95,
        },
      ],
    },
  ],
};

/** DEL 06:00 → GOI 09:20 direct: 3h 20m, more air time than the 1-stop but less elapsed. */
const DIRECT_SLOWER_AIR = {
  ...MOCK_OFFER,
  offer_id: "direct",
  total_inr: "4000",
  sell_inr: "4000",
  slices: [
    {
      segments: [
        {
          ...BASE_SEG,
          destination: "GOI",
          departure_at: "2025-09-01T06:00:00+05:30",
          arrival_at: "2025-09-01T09:20:00+05:30",
          duration_minutes: 200,
        },
      ],
    },
  ],
};

test.describe("Search flow", () => {
  test.beforeEach(async ({ page }) => {
    // Mock POST /v1/search
    await page.route("**/v1/search", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ search_id: SEARCH_ID }),
        });
      } else {
        await route.continue();
      }
    });

    // Mock GET /v1/search/:id/stream
    await page.route(`**/v1/search/${SEARCH_ID}/stream`, async (route) => {
      const events = [
        { type: "source_started", supplier_id: "mock_supplier", searched_at: new Date().toISOString() },
        {
          type: "offers_partial",
          supplier_id: "mock_supplier",
          offers_count: 1,
          groups: [MOCK_GROUP],
        },
        {
          type: "search_complete",
          groups: [MOCK_GROUP],
          suppliers_succeeded: ["mock_supplier"],
          suppliers_failed: [],
          suppliers_timed_out: [],
          suppliers_skipped: [],
          elapsed_seconds: 0.5,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
        body: events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n",
      });
    });
  });

  test("shows search form on load", async ({ page }) => {
    await gotoSearch(page);
    await expect(page.getByRole("button", { name: "Find available flights" })).toBeVisible();
  });

  test("submits search and shows supplier status strip", async ({ page }) => {
    await gotoSearch(page);

    await page.getByRole("combobox", { name: "Origin", exact: true }).fill("DEL");
    await page.getByRole("combobox", { name: "Destination", exact: true }).fill("BOM");

    await page.click("button[type='submit']");

    // Supplier status strip appears
    await expect(page.getByRole("status")).toBeVisible();
    await expect(page.getByRole("status").getByText("mock_supplier", { exact: true })).toBeVisible();
  });

  test("shows offer card after search completes", async ({ page }) => {
    await gotoSearch(page);

    await page.click("button[type='submit']");

    // Wait for offer card
    await expect(page.getByTestId("offer-card")).toBeVisible({ timeout: 10_000 });
    // Price is shown
    await expect(page.getByText("₹3,700")).toBeVisible();
  });

  test("shows UNKNOWN baggage as 'unknown' (not 'no')", async ({ page }) => {
    // Override with UNKNOWN baggage
    const unknownBaggageOffer = {
      ...MOCK_OFFER,
      offer_id: "offer-unknown-bags",
      baggage: { cabin_pieces: "UNKNOWN", cabin_kg: "UNKNOWN", checked_pieces: "UNKNOWN", checked_kg: "UNKNOWN" },
    };
    const unknownGroup = { ...MOCK_GROUP, dedupe_key: "unknownbags", primary: unknownBaggageOffer };

    await page.route(`**/v1/search/${SEARCH_ID}/stream`, async (route) => {
      const events = [
        { type: "source_started", supplier_id: "mock_supplier", searched_at: new Date().toISOString() },
        { type: "offers_partial", supplier_id: "mock_supplier", offers_count: 1, groups: [unknownGroup] },
        {
          type: "search_complete",
          groups: [unknownGroup],
          suppliers_succeeded: ["mock_supplier"],
          suppliers_failed: [],
          suppliers_timed_out: [],
          suppliers_skipped: [],
          elapsed_seconds: 0.1,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
        body: events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n",
      });
    });

    await gotoSearch(page);
    await page.click("button[type='submit']");
    await expect(page.getByTestId("offer-card")).toBeVisible({ timeout: 10_000 });

    // Should say "unknown" not "no"
    await expect(page.getByText(/Bags: unknown/)).toBeVisible();
    await expect(page.getByText("Bags: no")).not.toBeVisible();
  });

  test("opens offer drawer on 'View details'", async ({ page }) => {
    await gotoSearch(page);
    await page.click("button[type='submit']");

    await expect(page.getByTestId("offer-card")).toBeVisible({ timeout: 10_000 });
    await page.getByRole("button", { name: "View details" }).first().click();

    await expect(page.getByRole("dialog")).toBeVisible();
    await expect(page.getByText("Price breakdown")).toBeVisible();
  });

  test("closes drawer with Escape key", async ({ page }) => {
    await gotoSearch(page);
    await page.click("button[type='submit']");

    await expect(page.getByTestId("offer-card")).toBeVisible({ timeout: 10_000 });
    await page.getByRole("button", { name: "View details" }).first().click();
    await expect(page.getByRole("dialog")).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).not.toBeVisible();
  });

  test("sort modes reorder results", async ({ page }) => {
    // Provide two offers with different prices
    const cheapOffer = { ...MOCK_OFFER, offer_id: "cheap", total_inr: "2000", sell_inr: "2000" };
    const expensiveOffer = {
      ...MOCK_OFFER,
      offer_id: "expensive",
      total_inr: "5000",
  sell_inr: "5000",
      slices: [
        {
          segments: [
            {
              ...MOCK_OFFER.slices[0].segments[0],
              arrival_at: "2025-09-01T07:00:00+05:30",
              duration_minutes: 60, // faster
            },
          ],
        },
      ],
    };
    const twoGroups = [
      { dedupe_key: "expensive", primary: expensiveOffer, alternates: [] },
      { dedupe_key: "cheap", primary: cheapOffer, alternates: [] },
    ];

    await page.route(`**/v1/search/${SEARCH_ID}/stream`, async (route) => {
      const events = [
        { type: "source_started", supplier_id: "mock_supplier", searched_at: new Date().toISOString() },
        { type: "offers_partial", supplier_id: "mock_supplier", offers_count: 2, groups: twoGroups },
        {
          type: "search_complete",
          groups: twoGroups,
          suppliers_succeeded: ["mock_supplier"],
          suppliers_failed: [],
          suppliers_timed_out: [],
          suppliers_skipped: [],
          elapsed_seconds: 0.1,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
        body: events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n",
      });
    });

    await gotoSearch(page);
    await page.click("button[type='submit']");
    await expect(page.getByTestId("offer-card")).toHaveCount(2, { timeout: 10_000 });

    // Switch to cheapest — ₹2,000 should be first
    await page.getByRole("button", { name: "Cheapest" }).click();
    const cards = page.getByTestId("offer-card");
    await expect(cards.first()).toContainText("2,000");

    // Switch to fastest — 60 min offer (expensive) should be first
    await page.getByRole("button", { name: "Fastest" }).click();
    await expect(cards.first()).toContainText("5,000");
  });

  test("offer card shows elapsed journey time, layover included", async ({ page }) => {
    await mockStream(page, [{ dedupe_key: "one-stop", primary: ONE_STOP_LONG_LAYOVER, alternates: [] }]);

    await gotoSearch(page);
    await page.click("button[type='submit']");
    const card = page.getByTestId("offer-card");
    await expect(card).toBeVisible({ timeout: 10_000 });

    await expect(card).toContainText("5h 35m · 1 stop");
    await expect(card).not.toContainText("3h 5m");

    await page.getByRole("button", { name: "View details" }).click();
    const drawer = page.getByRole("dialog");
    await expect(drawer.getByTestId("slice-journey-time")).toHaveText("5h 35m total");
    await expect(drawer.getByTestId("layover")).toHaveText("Layover at BOM · 2h 30m");
    // Per-segment times stay air time.
    await expect(drawer.getByText("1h 30m flight")).toBeVisible();
    await expect(drawer.getByText("1h 35m flight")).toBeVisible();
  });

  test("elapsed time is correct across time zones", async ({ page }) => {
    // DEL 22:00 IST → DXB 00:30 GST next day = 4h elapsed (wall clocks differ by 1h 30m).
    const crossTz = {
      ...MOCK_OFFER,
      offer_id: "cross-tz",
      slices: [
        {
          segments: [
            {
              ...BASE_SEG,
              destination: "DXB",
              departure_at: "2025-09-01T22:00:00+05:30",
              arrival_at: "2025-09-02T00:30:00+04:00",
              duration_minutes: 240,
            },
          ],
        },
      ],
    };
    await mockStream(page, [{ dedupe_key: "cross-tz", primary: crossTz, alternates: [] }]);

    await gotoSearch(page);
    await page.click("button[type='submit']");
    await expect(page.getByTestId("offer-card")).toContainText("4h · Direct", { timeout: 10_000 });
  });

  test("fastest sort ranks a long layover after a direct flight", async ({ page }) => {
    await mockStream(page, [
      { dedupe_key: "one-stop", primary: ONE_STOP_LONG_LAYOVER, alternates: [] },
      { dedupe_key: "direct", primary: DIRECT_SLOWER_AIR, alternates: [] },
    ]);

    await gotoSearch(page);
    await page.click("button[type='submit']");
    const cards = page.getByTestId("offer-card");
    await expect(cards).toHaveCount(2, { timeout: 10_000 });

    await page.getByRole("button", { name: "Fastest" }).click();
    await expect(cards.first()).toContainText("3h 20m · Direct");
    await expect(cards.nth(1)).toContainText("5h 35m · 1 stop");
  });

  test("shows error on failed search start", async ({ page }) => {
    await page.route("**/v1/search", async (route) => {
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({ detail: [{ msg: "value is not a valid IATA code" }] }),
      });
    });

    await gotoSearch(page);
    await page.click("button[type='submit']");

    await expect(page.getByText(/value is not a valid IATA code/)).toBeVisible();
  });

  test("shows failed supplier in status strip", async ({ page }) => {
    await page.route(`**/v1/search/${SEARCH_ID}/stream`, async (route) => {
      const events = [
        { type: "source_started", supplier_id: "sabre", searched_at: new Date().toISOString() },
        { type: "source_failed", supplier_id: "sabre", reason: "auth_error" },
        {
          type: "search_complete",
          groups: [],
          suppliers_succeeded: [],
          suppliers_failed: ["sabre"],
          suppliers_timed_out: [],
          suppliers_skipped: [],
          elapsed_seconds: 0.1,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
        body: events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n",
      });
    });

    await gotoSearch(page);
    await page.click("button[type='submit']");

    await expect(page.getByText(/Failed/i)).toBeVisible({ timeout: 5_000 });
  });
});

test.describe("Airport autocomplete", () => {
  test("suggests airports by city and fills the IATA code", async ({ page }) => {
    await page.route("**/v1/airports?**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { iata: "BOM", name: "Chhatrapati Shivaji Maharaj International Airport", city: "Mumbai", country: "IN" },
        ]),
      }),
    );
    await gotoSearch(page);
    const to = page.getByRole("combobox", { name: "Destination", exact: true });
    await to.fill("mumb");
    await expect(page.getByRole("option", { name: /BOM Mumbai/ })).toBeVisible();
    await to.press("Enter");
    await expect(to).toHaveValue("BOM");
  });
});
