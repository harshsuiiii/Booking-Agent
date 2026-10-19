/**
 * Playwright tests for the agent chat → book → confirm flow.
 * The backend API is mocked via page.route() so no real server is needed.
 */

import { test, expect, type Page, type Request } from "@playwright/test";
import { signIn } from "./helpers";

const OFFER = {
  offer_id: "offer-xyz",
  rank: 1,
  is_test_data: false,
  airline: "AI",
  flight_number: "101",
  route: "DEL → BOM",
  departure: "2026-11-20T06:00:00+05:30",
  arrival: "2026-11-20T08:15:00+05:30",
  duration_minutes: 135,
  stops: 0,
  cabin: "ECONOMY",
  fare_basis: "YOW",
  fare_family: "SAVER",
  customer_price_inr: "7400",
  effective_price_inr: "7400",
  checked_baggage: "1",
  cabin_baggage: "1",
  refundable: "yes",
  changeable: "unknown",
  expires_at: "2026-11-19T00:00:00+00:00",
  pax_count: 2,
  source: "simulated",
  via: [],
};

/** Body of a /v1/agent/chat/stream response (Server-Sent Events). */
function sse(events: object[]): string {
  return events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
}

interface Captured {
  chat: unknown[];
  prepare: Record<string, unknown>[];
  confirm: unknown[];
}

async function mockApi(
  page: Page,
  opts: { prepareStatus?: number; prepareBody?: unknown } = {},
): Promise<Captured> {
  const captured: Captured = { chat: [], prepare: [], confirm: [] };
  const json = (r: Request) => JSON.parse(r.postData() ?? "{}");

  await page.route("**/v1/agent/chat/stream", async (route) => {
    captured.chat.push(json(route.request()));
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sse([
        { type: "session", session_id: "sess-1" },
        { type: "status", stage: "thinking", text: "Thinking…" },
        { type: "status", stage: "tool", tool: "search_flights", text: "Searching suppliers…" },
        { type: "tool_done", tool: "search_flights", ms: 900, text: "Found 42 offers from 3 supplier(s)." },
        { type: "reply", text: "**#1** is the cheapest direct option." },
        { type: "offers", offers: [OFFER] },
        { type: "done" },
      ]),
    });
  });

  await page.route("**/v1/bookings/prepare", async (route) => {
    captured.prepare.push(json(route.request()));
    await route.fulfill({
      status: opts.prepareStatus ?? 201,
      contentType: "application/json",
      body: JSON.stringify(
        opts.prepareBody ?? {
          booking_id: "bk-1",
          state: "PRICED",
          confirmation_token: "tok-secret",
          confirmation_token_expires_at: new Date(Date.now() + 300_000).toISOString(),
          customer_price_inr: "7400",
          offer_summary: {
            supplier_id: "demo",
            route: "DEL → BOM",
            departure: OFFER.departure,
            cabin: "ECONOMY",
            fare_basis: "YOW",
            valid_until: new Date(Date.now() + 600_000).toISOString(),
          },
        },
      ),
    });
  });

  await page.route("**/v1/bookings/bk-1/confirm", async (route) => {
    captured.confirm.push(json(route.request()));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        booking_id: "bk-1",
        tenant_id: "default",
        state: "TICKETED",
        pnr: "PNR123",
        supplier_booking_ref: "ord_1",
        passenger_count: 2,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        transitions: [],
      }),
    });
  });

  return captured;
}

async function askAndOpenBooking(page: Page) {
  await signIn(page);
  await page.goto("/assistant");
  await page.getByLabel("Message").fill("DEL to BOM on 20 Nov, 2 adults");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByTestId("assistant-message")).toContainText("cheapest direct option");
  const card = page.getByTestId("agent-offer-card");
  await expect(card).toContainText("₹7,400");
  await expect(card).toContainText("2 passengers");
  await expect(card.getByTestId("simulated-badge")).toHaveText("Simulated data");
  await card.getByRole("button", { name: "Book" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
}

async function fillPassengers(page: Page) {
  const dialog = page.getByRole("dialog");
  const names = [
    ["Asha", "Verma"],
    ["Ravi", "Verma"],
  ];
  for (let i = 0; i < names.length; i++) {
    const fs = dialog.getByRole("group", { name: `Passenger ${i + 1}` });
    await fs.getByLabel("Given name").fill(names[i][0]);
    await fs.getByLabel("Family name").fill(names[i][1]);
    await fs.getByLabel("Date of birth").fill("1990-05-01");
  }
}

test.describe("Agent chat booking", () => {
  test("books an offer through the UI form, never through the agent", async ({ page }) => {
    const captured = await mockApi(page);
    await askAndOpenBooking(page);
    await fillPassengers(page);

    await page.getByRole("button", { name: "Get live price" }).click();
    await expect(page.getByTestId("live-price")).toHaveText("₹7,400");
    await expect(page.getByText(/Confirm within \d:\d\d/)).toBeVisible();

    // Prepare got both passengers and an idempotency key.
    expect(captured.prepare).toHaveLength(1);
    expect(captured.prepare[0].passengers).toHaveLength(2);
    expect(captured.prepare[0].idempotency_key).toBeTruthy();
    // Nothing is booked until the separate confirm click.
    expect(captured.confirm).toHaveLength(0);

    await page.getByRole("button", { name: /Confirm and book/ }).click();
    await expect(page.getByTestId("booking-result")).toContainText("PNR123");
    expect(captured.confirm).toEqual([{ confirmation_token: "tok-secret" }]);

    // Passenger details never went to the chat endpoint.
    expect(JSON.stringify(captured.chat)).not.toContain("Asha");
  });

  test("shows a price change from prepare and does not offer confirm", async ({ page }) => {
    await mockApi(page, {
      prepareStatus: 409,
      prepareBody: {
        detail: {
          code: "price_changed",
          old_price_inr: "7400",
          new_price_inr: "8100",
          message: "Price changed",
        },
      },
    });
    await askAndOpenBooking(page);
    await fillPassengers(page);
    await page.getByRole("button", { name: "Get live price" }).click();

    await expect(page.getByRole("dialog").getByRole("alert")).toContainText("₹8,100");
    await expect(page.getByRole("button", { name: /Confirm and book/ })).toHaveCount(0);
  });

  test("shows streamed errors in the chat", async ({ page }) => {
    await page.route("**/v1/agent/chat/stream", (route) =>
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sse([
          { type: "session", session_id: "sess-1" },
          { type: "status", stage: "thinking", text: "Thinking…" },
          { type: "error", status: 502, message: "AI provider error (Groq): model not found" },
          { type: "done" },
        ]),
      }),
    );
    await signIn(page);
  await page.goto("/assistant");
    await page.getByLabel("Message").fill("hello");
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByTestId("chat-log").getByRole("alert")).toContainText("model not found");
  });

  test("shows progress while the assistant works", async ({ page }) => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    await page.route("**/v1/agent/chat/stream", async (route) => {
      await gate;
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sse([{ type: "session", session_id: "s" }, { type: "reply", text: "ok" }, { type: "done" }]),
      });
    });
    await signIn(page);
  await page.goto("/assistant");
    await page.getByLabel("Message").fill("hello");
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByTestId("agent-progress")).toBeVisible();
    release();
    await expect(page.getByTestId("assistant-message")).toHaveText("ok");
    await expect(page.getByTestId("agent-progress")).toHaveCount(0);
  });
});
