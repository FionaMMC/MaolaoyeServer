/* Browser contract tests against scripts.preview_blueprint ONLY.
 * Start the isolated loopback preview, then run with Playwright on NODE_PATH:
 * node --test tests/browser/blueprint_browser.test.cjs
 * No real server, QMT, credentials or trading requests are used.
 */
const { test, before, after, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const { mkdtemp } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const { chromium, expect } = require("playwright/test");
const base = new URL(
  process.env.BLUEPRINT_PREVIEW_URL ||
    "http://127.0.0.1:8765/dashboard/blueprint",
);
assert.equal(
  base.hostname,
  "127.0.0.1",
  "Only the isolated loopback preview is allowed",
);
assert.equal(base.pathname, "/dashboard/blueprint");
let browser, context, page, errors, requests, screenshots;
before(async () => {
  browser = await chromium.launch({
    headless: true,
    channel: process.env.BLUEPRINT_BROWSER_CHANNEL || "chrome",
  });
  screenshots = await mkdtemp(join(tmpdir(), "hydra-blueprint-qa-"));
  console.log("Screenshots:", screenshots);
});
after(async () => {
  await browser?.close();
});
beforeEach(async () => {
  context = await browser.newContext({
    viewport: { width: 1512, height: 982 },
  });
  page = await context.newPage();
  errors = [];
  requests = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) =>
    requests.push({ method: request.method(), url: request.url() }),
  );
});
afterEach(async () => {
  await context.close();
  assert.deepEqual(errors, [], "No uncaught browser errors");
  const unexpected = requests.filter((r) => {
    const u = new URL(r.url);
    return (
      u.origin !== base.origin ||
      ![
        "/dashboard/blueprint",
        "/dashboard/blueprint/assets/blueprint.css",
        "/dashboard/blueprint/assets/model.js",
        "/dashboard/blueprint/assets/blueprint.js",
        "/admin/architecture-blueprint/session",
        "/admin/architecture-blueprint/decisions/g09",
      ].includes(u.pathname)
    );
  });
  assert.deepEqual(unexpected, [], "No external or trading requests");
});
async function open(hash = "journey?mode=live&scenario=normal&step=0") {
  await page.goto(base.href + "#" + hash);
  await expect(page.locator("#step-title")).not.toBeEmpty();
}
async function chapter(view) {
  await page.locator('.section-nav [data-view="' + view + '"]').click();
}
async function amount(id, value) {
  await expect(page.locator("#" + id)).toHaveText(value);
}
async function noOverflow() {
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
}

test("normal flow: reservation is not a loss, fills and mark-to-market reconcile", async () => {
  await open("journey?mode=live&scenario=normal&step=4");
  await amount("cash-value", "¥211,000");
  await amount("nav-value", "¥211,000");
  await amount("held-value", "¥100,050");
  await amount("free-value", "¥110,950");
  await amount("pnl-value", "¥0");
  await page.locator("#next").click();
  await page.locator("#next").click();
  await amount("cash-value", "¥110,950");
  await amount("position-value", "¥100,000");
  await amount("nav-value", "¥210,950");
  await page.locator('#timeline [data-step="9"]').click();
  await amount("nav-value", "¥212,950");
  await amount("pnl-value", "+¥1,950");
  await expect(page.locator("#next")).toBeDisabled();
  await page.locator("#show-math").click();
  await expect(page.locator("#detail-dialog")).toContainText("未承诺");
  await expect(page.locator("#detail-dialog")).toContainText(
    "不是实际 ETF 价格",
  );
});

test("deposit increases only reserve and does not invalidate the existing demonstration grant", async () => {
  await open();
  await page.locator("#scenario-select").selectOption("deposit");
  await amount("reserve-value", "¥20,000,000");
  await amount("capital-value", "¥211,000");
  await amount("nav-value", "¥211,000");
  await expect(page.locator("#scenario-callout")).toContainText("什么必须继续");
  await expect(page.locator("#next")).toBeEnabled();
});

test("server outage preserves local facts, recovery never submits a second order", async () => {
  await open();
  await page.locator("#scenario-select").selectOption("server-offline");
  await page.locator('#timeline [data-step="6"]').click();
  await expect(page.locator("#next")).toBeDisabled();
  await expect(page.locator("#runtime-status")).toContainText("server 未入账");
  await page.locator("#recover-scenario").click();
  await expect(page.locator("#runtime-status")).toContainText("原事实只记一次");
  await page.locator('#timeline [data-step="9"]').click();
  await page.locator("#show-math").click();
  await expect(page.locator("#detail-dialog")).toContainText(
    "BUY 调用演示次数：1",
  );
  assert.equal(
    requests.some((r) => r.method !== "GET"),
    false,
  );
});

test("pending remainder blocks only conflicting residual until terminal evidence arrives", async () => {
  await open();
  await page.locator("#scenario-select").selectOption("pending");
  await amount("held-value", "¥60,030");
  await amount("position-value", "¥40,000");
  await expect(page.locator("#next")).toBeDisabled();
  await expect(page.locator("#runtime-status")).toContainText("余 600 仍已报");
  await expect(page.locator('#timeline [data-step="8"]')).toBeDisabled();
  await page.locator("#recover-scenario").click();
  await amount("held-value", "¥0");
  await page.locator("#next").click();
  await expect(page.locator("#step-title")).toContainText("600 份");
  await page.locator("#next").click();
  await amount("nav-value", "¥211,780");
});

test("rejected sale never borrows the other nineteen million; unknown cash is not shown as reconciled", async () => {
  await open();
  await page.locator("#scenario-select").selectOption("sell-rejected");
  await amount("cash-value", "¥0");
  await amount("position-value", "¥211,000");
  await expect(page.locator("#next")).toBeDisabled();
  await page.locator("#show-math").click();
  await expect(page.locator("#detail-dialog")).toContainText(
    "BUY 调用演示次数：0；SELL：1",
  );
  await page.keyboard.press("Escape");
  await page.locator("#scenario-select").selectOption("cash-gap");
  await amount("nav-value", "待核实");
  await amount("pnl-value", "不可确认");
  await expect(page.locator("#conservation-label")).toContainText(
    "没有足够证据",
  );
});

test("recovering a receipt does not reserve twice; returning authority cannot reuse the old package", async () => {
  await open();
  await page.locator("#scenario-select").selectOption("receipt-lost");
  await amount("held-value", "¥100,050");
  await page.locator("#recover-scenario").click();
  await amount("held-value", "¥100,050");
  await page.locator("#scenario-select").selectOption("grant-recall");
  await page.locator("#recover-scenario").click();
  await amount("held-value", "¥0");
  await amount("nav-value", "¥211,000");
  await expect(page.locator("#next")).toBeDisabled();
  await expect(page.locator("#runtime-status")).toContainText("原包不可再提交");
});

test("late fees adjust only thirty cents and notification retry never redoes the financial flow", async () => {
  await open();
  await page.locator("#scenario-select").selectOption("fees");
  await amount("held-value", "¥10");
  await amount("nav-value", "¥212,950");
  await expect(page.locator("#runtime-status")).toContainText("暂估");
  await page.locator("#recover-scenario").click();
  await amount("fee-value", "¥50.30");
  await amount("nav-value", "¥212,949.70");
  await amount("held-value", "¥0");
  await page.locator("#scenario-select").selectOption("notify-failed");
  const before = await page.locator("#nav-value").textContent();
  await page.locator("#recover-scenario").click();
  await amount("nav-value", before);
  await expect(page.locator("#runtime-status")).toContainText("通知重试成功");
});

test("shadow and paper explain different evidence; shadow has no live execution gate", async () => {
  await open("journey?mode=live&scenario=normal&step=5");
  await page.getByRole("button", { name: "影子盘", exact: true }).click();
  await expect(page.locator("#flow-map svg")).toContainText("虚拟撮合器");
  await expect(page.locator("#flow-map svg")).not.toContainText("本地 MiniQMT");
  await expect(page.locator('#stage-gates [data-item="g02"]')).toBeVisible();
  assert.equal(
    await page
      .locator('#scenario-select option[value="server-offline"]')
      .count(),
    0,
  );
  await page.getByRole("button", { name: "QMT 模拟盘", exact: true }).click();
  await expect(page.locator("#flow-map svg")).toContainText("QMT 模拟账户");
  await chapter("worlds");
  await expect(page.locator(".world-card")).toHaveCount(3);
});
test("loss and dividend stories keep the capital boundary and current-step figures consistent", async () => {
  await open();
  await page.locator("#scenario-select").selectOption("drawdown");
  await amount("nav-value", "¥208,950");
  await amount("pnl-value", "−¥2,050");
  await amount("step-nav", "¥208,950");
  await amount("reserve-value", "¥19,000,000");
  await page.locator("#scenario-select").selectOption("dividend");
  await amount("cash-value", "¥111,450");
  await amount("position-value", "¥99,500");
  await amount("nav-value", "¥210,950");
  await amount("capital-value", "¥211,000");
  await expect(page.locator("#scenario-callout")).toContainText(
    "不考虑税和其他市场变动",
  );
});

test("gate search and local discussion persist safely without any network write", async () => {
  await open();
  await chapter("gates");
  await expect(page.locator(".gate-card")).toHaveCount(26);
  await page.locator("#gate-search").fill("G09");
  await expect(page.locator(".gate-card")).toHaveCount(1);
  await page.locator(".gate-card").click();
  await expect(page.locator("#detail-dialog")).toContainText("什么必须继续");
  await page.locator("#note-author").fill("规则同伴");
  await page.locator("#note-status").selectOption("change_required");
  const note = '<img src=x onerror="window.injected=true"> 预留不扣净值。';
  await page.locator("#note-rationale").fill(note);
  await page.locator("#save-note").click();
  await expect(page.locator("#toast")).toContainText("尚未共享");
  await page.keyboard.press("Escape");
  await page.reload();
  await chapter("discussion");
  await expect(page.locator("#minutes")).toContainText(note);
  assert.equal(await page.evaluate(() => window.injected), undefined);
  assert.equal(
    requests.some((r) => r.method !== "GET"),
    false,
  );
});

test("shared review uses explicit review-only writes and never exports or persists its credential", async () => {
  const testKey = "REVIEW_TEST_ONLY_NOT_A_SECRET";
  const session = {
    session_id: "three-books-blueprint-20260907-v1",
    decisions: [],
    comments: [],
    updated_at: null,
  };
  let saved;
  await page.route("**/admin/architecture-blueprint/**", async (route) => {
    const request = route.request();
    assert.equal(request.headers().authorization, "Bearer " + testKey);
    if (request.method() === "PUT") {
      saved = {
        ...request.postDataJSON(),
        item_id: "g09",
        updated_at: "2026-09-07T08:00:00+00:00",
      };
      await route.fulfill({ json: { code: 0, message: "ok", data: saved } });
    } else
      await route.fulfill({ json: { code: 0, message: "ok", data: session } });
  });
  await open();
  await chapter("discussion");
  await page.locator("#connect-notes").click();
  await page.locator("#notes-key").fill(testKey);
  await page.locator("#connect-submit").click();
  await expect(page.locator("#notes-status")).toContainText("共享讨论已连接");
  await chapter("gates");
  await page.locator('#gate-grid [data-item="g09"]').click();
  await page.locator("#note-author").fill("规则同伴");
  await page.locator("#note-rationale").fill("使用权与所有权分别展示。");
  await page.locator("#save-note").click();
  await expect(page.locator("#toast")).toContainText("共享设计意见已保存");
  assert.equal(saved.rationale, "使用权与所有权分别展示。");
  await page.keyboard.press("Escape");
  const downloadPromise = page.waitForEvent("download");
  await page.locator("#export-notes").click();
  const download = await downloadPromise;
  const stream = await download.createReadStream();
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  const markdown = Buffer.concat(chunks).toString();
  assert.match(markdown, /### G26 /);
  assert.match(markdown, /使用权与所有权分别展示/);
  assert.equal(markdown.includes(testKey), false);
  assert.equal(
    await page.evaluate(
      (k) => JSON.stringify({ ...localStorage }).includes(k),
      testKey,
    ),
    false,
  );
  assert.equal(page.url().includes(testKey), false);
  await chapter("discussion");
  await page.locator("#connect-notes").click();
  await page.locator("#disconnect-notes").click();
  await expect(page.locator("#notes-status")).toContainText("尚未共享");
});

test("failed review connection leaves read-only exploration usable and does not announce success", async () => {
  await page.route("**/admin/architecture-blueprint/session", (route) =>
    route.fulfill({
      status: 401,
      json: { code: 401, message: "测试凭据无效" },
    }),
  );
  await open();
  await chapter("discussion");
  await page.locator("#connect-notes").click();
  await page.locator("#notes-key").fill("INVALID_TEST_VALUE");
  await page.locator("#connect-submit").click();
  await expect(page.locator("#toast")).toContainText("未连接");
  await page.keyboard.press("Escape");
  await chapter("journey");
  await page.locator("#next").click();
  await amount("cash-value", "¥211,000");
});

test("playback pauses its motion, keyboard and reduced-motion work, hashes restore safely", async () => {
  await open("journey?mode=live&scenario=normal&step=1");
  await page.locator("#speed").selectOption("1200");
  await page.locator("#play").click();
  await expect(page.locator("#play-label")).toHaveText("暂停播放");
  await expect(page.locator("#flow-map animateMotion")).toHaveCount(1);
  await page.waitForFunction(() => location.hash.includes("step=2"));
  await page.locator("#play").click();
  await expect(page.locator("#flow-map animateMotion")).toHaveCount(0);
  await page.locator('#flow-map svg [data-step="4"]').focus();
  await page.keyboard.press("Enter");
  await amount("held-value", "¥100,050");
  await page.reload();
  await amount("held-value", "¥100,050");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.locator("#play").click();
  await expect(page.locator("#flow-map animateMotion")).toHaveCount(0);
  await page.locator("#play").click();
  await open("journey?mode=__proto__&scenario=constructor&step=Infinity");
  await amount("cash-value", "¥0");
  await expect(page.locator("#scenario-select")).toHaveValue("normal");
});

test("desktop, tablet and phone layouts cover every chapter without horizontal overflow", async () => {
  await open();
  for (const width of [1512, 820, 390]) {
    await page.setViewportSize({ width, height: 982 });
    for (const view of [
      "journey",
      "worlds",
      "scenarios",
      "gates",
      "discussion",
    ]) {
      await chapter(view);
      await noOverflow();
      await page.screenshot({
        path: join(screenshots, `${width}-${view}.png`),
        fullPage: true,
        animations: "disabled",
      });
    }
  }
  await chapter("journey");
  await expect(page.locator(".compact-map")).toBeVisible();
  await expect(page.locator("#flow-map svg")).toBeHidden();
  await page.locator('.compact-map [data-step="4"]').click();
  await amount("held-value", "¥100,050");
  await page.locator("#discuss-step").click();
  await noOverflow();
  await page.screenshot({
    path: join(screenshots, "390-dialog.png"),
    fullPage: true,
    animations: "disabled",
  });
});
