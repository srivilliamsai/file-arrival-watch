import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";
import { randomUUID } from "node:crypto";

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Today’s delivery" })).toBeVisible();
});

for (const width of [320, 390, 768, 1024, 1440]) {
  test(`dashboard fits ${width}px and exposes delivery history`, async ({ page }) => {
    await page.setViewportSize({ width, height: width < 768 ? 844 : 1024 });
    await expect(page.getByRole("heading", { name: "Missing stock file" })).toBeVisible();
    const timeDisplay = page.getByRole("combobox", { name: "Show times in" });
    const timeZone = page.locator("#display-time-zone");
    await expect(timeZone).toHaveText("Asia/Kolkata");
    const controlBounds = await timeDisplay.boundingBox();
    const zoneBounds = await timeZone.boundingBox();
    expect(controlBounds.height).toBeGreaterThanOrEqual(44);
    if (width < 768) {
      expect(zoneBounds.x).toBeCloseTo(controlBounds.x);
      expect(zoneBounds.y).toBeGreaterThanOrEqual(controlBounds.y + controlBounds.height);
    }
    await timeDisplay.click();
    const timeOptions = page.getByRole("listbox", { name: "Show times in" });
    await expect(timeOptions).toBeVisible();
    const menuBounds = await timeOptions.boundingBox();
    expect(menuBounds.x).toBeCloseTo(controlBounds.x);
    expect(menuBounds.width).toBeCloseTo(controlBounds.width);
    expect(menuBounds.y).toBeGreaterThan(controlBounds.y + controlBounds.height);
    expect(menuBounds.x + menuBounds.width).toBeLessThanOrEqual(width);
    await expect(timeOptions).toHaveCSS("background-color", "rgb(255, 255, 255)");
    await page.keyboard.press("Escape");
    await expect(timeOptions).toBeHidden();
    const signIn = page.getByRole("button", { name: "Operator sign in" });
    await expect(signIn).toBeVisible();
    if (width < 768) expect((await signIn.boundingBox()).height).toBeGreaterThanOrEqual(44);
    const historyLink = page.getByRole("link", { name: "History", exact: true });
    const todayLink = page.getByRole("link", { name: "Today", exact: true });
    await historyLink.click();
    await expect(page.getByRole("heading", { name: "Delivery history" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Today’s delivery" })).toBeHidden();
    await expect(historyLink).toHaveAttribute("aria-current", "location");
    expect(await page.evaluate(() => scrollY)).toBe(0);
    await expect(page.locator("#history-rows tr")).toHaveCount(8);
    const desktop = page.getByRole("table");
    const mobile = page.getByRole("list", { name: "Warehouse delivery history" });
    if (width >= 768) {
      await expect(desktop).toBeVisible();
      await expect(mobile).toBeHidden();
    } else {
      await expect(desktop).toBeHidden();
      await expect(mobile).toBeVisible();
    }
    const overflows = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    expect(overflows).toBe(false);
    if ([390, 1440].includes(width)) {
      await page.evaluate(() => document.fonts.ready);
      const audit = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
      expect(audit.violations.map(({ id, nodes }) => ({ id, nodes: nodes.map((node) => node.target) }))).toEqual([]);
      await mkdir("docs/screenshots", { recursive: true });
      await page.screenshot({ path: `docs/screenshots/${width === 390 ? "mobile" : "desktop"}-history.png`, fullPage: true });
    }
    await todayLink.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: "Today’s delivery" })).toBeVisible();
    await expect(todayLink).toHaveAttribute("aria-current", "location");
    expect(await page.evaluate(() => scrollY)).toBe(0);
    if ([390, 1440].includes(width)) {
      await page.evaluate(() => document.fonts.ready);
      const fonts = await page.evaluate(() => [...document.fonts].map(({ family, status }) => [family, status]));
      expect(fonts).toEqual(expect.arrayContaining([["Inter", "loaded"], ["Inter Tight", "loaded"]]));
      const audit = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
      expect(audit.violations.map(({ id, nodes }) => ({ id, nodes: nodes.map((node) => node.target) }))).toEqual([]);
      await mkdir("docs/screenshots", { recursive: true });
      await page.screenshot({ path: `docs/screenshots/${width === 390 ? "mobile" : "desktop"}.png`, fullPage: true });
    }
  });
}

test("History deep links, skips to its content, and works with browser Back", async ({ page }) => {
  await page.goto("/#history");
  await expect(page.getByRole("heading", { name: "Delivery history" })).toBeVisible();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main")).toBeFocused();
  await expect(page).toHaveURL(/#history$/);
  await page.getByRole("link", { name: "Today", exact: true }).click();
  await page.goBack();
  await expect(page.getByRole("heading", { name: "Delivery history" })).toBeVisible();
  await expect(page.getByRole("link", { name: "History", exact: true })).toHaveAttribute("aria-current", "location");
});

test("history keeps today and recorded days before and after operator sign-in", async ({ page }) => {
  const current = {
    date: "2026-11-16", status: "awaiting_file", reason: "Today’s stock file is due by the warehouse cutoff.",
    cutoffAt: "2026-11-16T17:30:00Z", receivedAt: null, latestUploadAt: null, deadlineOutcome: null, attempts: 0,
  };
  await page.route("**/api/status", (route) => route.fulfill({ json: {
    warehouse: "South Warehouse", timeZone: "Asia/Kolkata", today: current,
    history: [
      { ...current, date: "2026-11-15", cutoffAt: "2026-11-15T17:30:00Z", status: "missing", deadlineOutcome: "missing" },
      { ...current, date: "2026-11-14", cutoffAt: "2026-11-14T17:30:00Z", status: "not_monitored" },
      { ...current, date: "2026-11-13", cutoffAt: null, status: "cutoff_skipped" },
    ],
  } }));
  await page.reload();
  await expect(page.getByRole("heading", { name: "Awaiting file" })).toBeVisible();
  const mark = page.locator("#state-mark");
  await expect(mark).toBeEmpty();
  expect(await mark.evaluate((element) => getComputedStyle(element, "::before").borderLeftWidth)).toBe("2px");
  const history = page.getByRole("link", { name: "History", exact: true });
  await history.click();
  await expect(page.locator("#history-rows tr")).toHaveCount(2);
  await expect(page.locator("#history-rows")).toContainText("Awaiting file");
  await expect(page.locator("#history-rows")).toContainText("15 Nov 2026");
  await expect(page.locator("#history-rows")).toContainText("Missing");
  await expect(page.locator("#history-mobile > li")).toHaveCount(2);
  await page.getByRole("link", { name: "Today", exact: true }).click();
  await page.getByRole("button", { name: "Operator sign in" }).click();
  await page.getByRole("button", { name: "Sign in to demo" }).click();
  await expect(page.getByLabel("Choose a CSV file")).toBeVisible();
  await history.click();
  await expect(page.locator("#history-rows tr")).toHaveCount(2);
  await expect(page.locator("#history-mobile > li")).toHaveCount(2);
});

for (const width of [320, 390, 768, 1024, 1440]) {
  test(`operator sign-in is centered and fits at ${width}px`, async ({ page }) => {
    const height = width === 320 ? 568 : 844;
    await page.setViewportSize({ width, height });
    const openSignIn = page.getByRole("button", { name: "Operator sign in" });
    await openSignIn.click();
    await expect(page).toHaveURL(/#operator-sign-in$/);
    await expect(page.getByRole("heading", { name: "Operator sign in" })).toBeFocused();
    await expect(page.getByRole("navigation", { name: "Dashboard" })).toBeHidden();
    await expect(page.getByRole("heading", { name: "Today’s delivery" })).toBeHidden();
    await expect(page.getByRole("button", { name: "Sign in to demo" })).toBeEnabled();
    const panel = await page.getByRole("region", { name: "Operator sign in" }).boundingBox();
    expect(Math.abs(panel.x + panel.width / 2 - width / 2)).toBeLessThanOrEqual(1);
    expect(Math.abs(panel.y + panel.height / 2 - height / 2)).toBeLessThanOrEqual(1);
    expect((await page.getByRole("button", { name: "Sign in to demo" }).boundingBox()).height).toBeGreaterThanOrEqual(44);
    expect((await page.getByRole("link", { name: "Back to dashboard" }).boundingBox()).height).toBeGreaterThanOrEqual(44);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    if ([390, 1440].includes(width)) {
      await page.evaluate(() => document.fonts.ready);
      const audit = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
      expect(audit.violations.map(({ id, nodes }) => ({ id, nodes: nodes.map((node) => node.target) }))).toEqual([]);
      await mkdir("docs/screenshots", { recursive: true });
      await page.screenshot({ path: `docs/screenshots/${width === 390 ? "mobile" : "desktop"}-sign-in.png` });
    }
    await page.goBack();
    await expect(openSignIn).toBeFocused();
    await expect(page.getByRole("heading", { name: "Today’s delivery" })).toBeVisible();
  });
}

test("sign-in hands off to Cognito with PKCE after the operator continues", async ({ page, baseURL }) => {
  const authUrl = "https://cognito.example.test";
  const redirectUri = new URL("/", baseURL).href;
  await page.route("**/config.json", (route) => route.fulfill({ json: { apiUrl: "", authUrl, clientId: "operator-client", redirectUri } }));
  await page.route(authUrl + "/oauth2/authorize?*", (route) => route.fulfill({ body: "Cognito sign-in" }));
  await page.reload();
  await page.getByRole("button", { name: "Operator sign in" }).click();
  await expect(page).toHaveURL(redirectUri + "#operator-sign-in");
  const authorization = page.waitForRequest(authUrl + "/oauth2/authorize?*");
  await page.getByRole("button", { name: "Continue to AWS sign-in" }).click();
  const params = new URL((await authorization).url()).searchParams;
  expect(params.get("response_type")).toBe("code");
  expect(params.get("client_id")).toBe("operator-client");
  expect(params.get("redirect_uri")).toBe(redirectUri);
  expect(params.get("scope")).toBe("file-arrival/upload");
  expect(params.get("code_challenge_method")).toBe("S256");
  expect(params.get("code_challenge")).toMatch(/^[\w-]{43}$/);
  expect(params.get("state")).toMatch(/^[\w-]{43}$/);
});

test("public dashboard recovers when status request fails", async ({ page }) => {
  await page.route("**/api/status", (route) => route.fulfill({ status: 503, json: { message: "Unavailable" } }));
  await page.reload();
  await expect(page.getByRole("heading", { name: "Status unavailable" })).toBeVisible();
  await page.unroute("**/api/status");
  await page.getByRole("button", { name: "Retry status check" }).click();
  await expect(page.getByRole("heading", { name: "Missing stock file" })).toBeVisible();
});

test("mobile operator rejects a stale CSV and accepts a correction", async ({ page, browser }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.getByRole("button", { name: "Operator sign in" }).focus();
  await page.keyboard.press("Enter");
  const demoSignIn = page.getByRole("button", { name: "Sign in to demo" });
  await page.keyboard.press("Tab");
  await expect(demoSignIn).toBeFocused();
  await page.keyboard.press("Enter");
  const input = page.getByLabel("Choose a CSV file");
  await expect(input).toBeVisible();
  const today = await page.locator("#delivery-date").getAttribute("datetime");
  const header = "snapshot_date,sku,quantity\n";
  await input.setInputFiles({ name: "stale.csv", mimeType: "text/csv", buffer: Buffer.from(header + "2020-01-01,STOCK-101,24\n") });
  await page.getByRole("button", { name: "Upload CSV" }).click();
  await expect(page.getByRole("status")).toContainText("File rejected");
  await expect(page.getByText("Row 2 · Snapshot date must be " + today + ".")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Invalid stock file" })).toBeVisible();
  await input.setInputFiles({ name: "incomplete.csv", mimeType: "text/csv", buffer: Buffer.from(header + today + ",STOCK-101,24\n") });
  await page.getByRole("button", { name: "Upload CSV" }).click();
  await expect(page.locator("#upload-errors")).toContainText("Required SKUs missing: STOCK-102.");
  await expect(page.getByRole("heading", { name: "Invalid stock file" })).toBeVisible();
  await input.setInputFiles({
    name: "corrected.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(header + today + ",STOCK-101,24\n" + today + ",STOCK-102,0\n"),
  });
  await page.getByRole("button", { name: "Upload CSV" }).click();
  await expect(page.getByRole("status")).toContainText("File accepted");
  await expect(page.getByRole("heading", { name: "Ready · received late" })).toBeVisible();
  await page.screenshot({ path: "docs/screenshots/mobile-ready.png", fullPage: true });
  await page.reload();
  await page.getByText("Today’s upload activity", { exact: true }).click();
  await expect(page.locator("#activity-results > li")).toHaveCount(3);
  await expect(page.locator("#activity-results")).toContainText("Required SKUs missing: STOCK-102.");
  await expect(page.locator("#activity-results")).toContainText("2 stock rows validated");
  await expect(page.locator("#activity-results > li").first()).toContainText("Uploaded ");
  await expect(page.locator("#activity-results > li").first()).toContainText(" IST");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  const signedOut = await browser.newPage({ viewport: { width: 1440, height: 1024 } });
  await signedOut.goto(new URL("/", page.url()).href);
  await expect(signedOut.getByRole("heading", { name: "Ready · received late" })).toBeVisible();
  await expect(signedOut.getByLabel("Choose a CSV file")).toBeHidden();
  await expect(signedOut.getByText("Today’s upload activity", { exact: true })).toBeHidden();
  await signedOut.close();
});

test("a later upload updates activity and today's history without replacing the first valid receipt", async ({ page }) => {
  await page.getByRole("button", { name: "Operator sign in" }).click();
  await page.getByRole("button", { name: "Sign in to demo" }).click();
  await expect(page.getByLabel("Choose a CSV file")).toBeVisible();
  const session = await (await page.request.post("/local/session")).json();
  const today = await page.locator("#delivery-date").getAttribute("datetime");
  const csv = "snapshot_date,sku,quantity\n" + today + ",STOCK-101,24\n";
  const submit = (content) => page.request.post("/api/uploads", {
    headers: { Authorization: "Bearer " + session.token },
    data: { uploadId: today + "_" + randomUUID(), csv: content },
  });
  expect((await submit(csv + today + ",STOCK-102,0\n")).status()).toBe(202);
  await page.getByText("Today’s upload activity", { exact: true }).click();
  const activity = page.locator("#activity-results > li");
  await expect(activity.first()).toContainText("Accepted");
  const initialCount = await activity.count();
  await expect(page.locator("#received")).toContainText(" IST", { timeout: 18000 });

  const firstValid = await page.locator("#received").textContent();
  expect((await submit(csv)).status()).toBe(202);

  const status = await (await page.request.get("/api/status")).json();
  const latestTime = new Intl.DateTimeFormat("en-IN", {
    hour: "numeric", minute: "2-digit", second: "2-digit", timeZone: "Asia/Kolkata", hour12: true,
  }).format(new Date(status.today.latestUploadAt)).replace(/\s+/g, " ").toUpperCase() + " IST";
  await expect(activity).toHaveCount(initialCount + 1, { timeout: 18000 });
  await expect(activity.first()).toContainText("Rejected · Uploaded " + latestTime);
  await expect(page.locator("#latest-upload")).toHaveText(latestTime);
  await expect(page.locator("#received")).toHaveText(firstValid);
  await page.getByRole("link", { name: "History", exact: true }).click();
  await expect(page.locator("#history-rows tr").first()).toContainText(latestTime);
});

test("local time conversion preserves the warehouse date and saved display choice", async ({ browser, baseURL }) => {
  const page = await browser.newPage({ timezoneId: "America/New_York", viewport: { width: 320, height: 844 } });
  const warehouseDate = "2026-03-09";
  const receivedAt = `${warehouseDate}T02:33:12Z`;
  const delivery = {
    date: warehouseDate, status: "ready_late", reason: "The stock file passed validation.",
    cutoffAt: `${warehouseDate}T02:30:00Z`, receivedAt, latestUploadAt: receivedAt, deadlineOutcome: "missing",
  };
  await page.route("**/api/status", (route) => route.fulfill({ json: {
    warehouse: "South Warehouse", timeZone: "Asia/Kolkata", today: delivery,
    history: [{ ...delivery, date: "2026-03-07", cutoffAt: "2026-03-07T02:30:00Z", receivedAt: null, latestUploadAt: null, status: "missing" }],
  } }));
  await page.route("**/api/uploads", (route) => route.fulfill({ json: { uploads: [{
    uploadId: `${warehouseDate}_${randomUUID()}`, status: "accepted", receivedAt, validatedAt: receivedAt, rowCount: 3, errors: [],
  }] } }));
  await page.goto(baseURL);
  const timeDisplay = page.getByRole("combobox", { name: "Show times in" });
  const timeOptions = page.getByRole("listbox", { name: "Show times in" });
  await expect(timeDisplay).toHaveText("Warehouse time");
  await expect(page.locator("#cutoff")).toHaveText("8:00 AM IST");
  await timeDisplay.focus();
  await page.keyboard.press("Space");
  await expect(timeDisplay).toBeFocused();
  await expect(timeDisplay).toHaveCSS("outline-offset", "-2px");
  await expect(timeOptions).toBeVisible();
  await page.keyboard.press("ArrowDown");
  await expect(timeDisplay).toHaveAttribute("aria-activedescendant", "time-local");
  await page.keyboard.press("Escape");
  await expect(timeDisplay).toHaveText("Warehouse time");
  await expect(timeOptions).toBeHidden();
  await page.getByRole("button", { name: "Operator sign in" }).click();
  await page.getByRole("button", { name: "Sign in to demo" }).click();
  await page.getByText("Today’s upload activity", { exact: true }).click();
  await expect(page.locator("#activity-results")).toContainText("Uploaded 8:03:12 AM IST");

  await timeDisplay.focus();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(timeDisplay).toHaveText("My local time");
  await expect(timeOptions).toBeHidden();
  await expect(page.locator("#display-time-zone")).toHaveText("America/New_York");
  await expect(page.locator("#cutoff")).toHaveText("8 Mar 2026 · 10:30 PM GMT-4");
  await expect(page.locator("#activity-results")).toContainText("Uploaded 8 Mar 2026 · 10:33:12 PM GMT-4");
  await expect(page.locator("#delivery-date")).toHaveAttribute("datetime", warehouseDate);
  await expect(page.getByRole("heading", { name: "Ready · received late" })).toBeVisible();

  await page.getByRole("link", { name: "History", exact: true }).click();
  await expect(page.locator("#history-mobile > li").last()).toContainText("6 Mar 2026 · 9:30 PM GMT-5");
  await page.reload();
  await expect(timeDisplay).toHaveText("My local time");
  await expect(page.locator("#history-mobile > li").first()).toContainText("8 Mar 2026 · 10:30 PM GMT-4");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  await timeDisplay.click();
  await timeOptions.getByRole("option", { name: "Warehouse time" }).click();
  await expect(timeDisplay).toHaveText("Warehouse time");
  await expect(timeOptions).toBeHidden();
  await expect(page.locator("#history-mobile > li").first()).toContainText("Due 8:00 AM IST");
  await page.close();
});
