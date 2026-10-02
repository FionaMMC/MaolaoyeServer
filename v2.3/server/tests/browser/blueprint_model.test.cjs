const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "../../app/web/blueprint");
const M = require(path.join(root, "model.js"));
const C = JSON.parse(fs.readFileSync(path.join(root, "catalog.json"), "utf8"));

test("catalog and accounting model have the same scenarios and applicability", () => {
  assert.deepEqual(
    Object.keys(M.SCENARIOS).sort(),
    C.scenarios.map((s) => s.id).sort(),
  );
  for (const s of C.scenarios)
    assert.deepEqual([...M.SCENARIOS[s.id].modes].sort(), [...s.modes].sort());
});
test("all supported story steps conserve ownership and use safe integer cents", () => {
  for (const scenario of C.scenarios)
    for (const mode of scenario.modes)
      for (const resolved of [false, true])
        for (let step = 0; step < 10; step++) {
          const s = M.snapshot({ mode, scenario: scenario.id, step, resolved });
          assert.equal(
            s.conserved,
            true,
            `${mode} ${scenario.id} ${step} ${resolved}`,
          );
          for (const key of [
            "reserve",
            "cash",
            "hold",
            "available",
            "marketValue",
            "nav",
            "fee",
            "accountNav",
          ]) {
            assert.ok(Number.isSafeInteger(s[key]), key);
            assert.ok(s[key] >= 0, key);
          }
          assert.equal(s.available + s.hold, s.cash);
          assert.equal(s.cash + s.marketValue, s.nav);
        }
});
test("allocating ownership and reserving cash cannot manufacture a loss", () => {
  const before = M.snapshot({ step: 0 }),
    allocation = M.snapshot({ step: 1 }),
    frozen = M.snapshot({ step: 4 });
  assert.equal(before.accountNav, allocation.accountNav);
  assert.equal(allocation.nav, frozen.nav);
  assert.equal(frozen.cash, 21100000);
  assert.equal(frozen.hold, 10005000);
  assert.equal(frozen.available, 11095000);
  assert.equal(frozen.pnl, 0);
});
test("extra account deposit increases reserve, not strategy budget or earnings", () => {
  const regular = M.snapshot({ step: 5 }),
    deposit = M.snapshot({ scenario: "deposit", step: 5 });
  assert.equal(deposit.reserve - regular.reserve, 100000000);
  for (const key of ["cash", "hold", "allocation", "nav", "pnl"])
    assert.equal(deposit[key], regular[key]);
});
test("full fill, cost and subsequent price change produce consistent NAV", () => {
  const filled = M.snapshot({ step: 6 }),
    marked = M.snapshot({ step: 9 });
  assert.equal(filled.cash, 11095000);
  assert.equal(filled.marketValue, 10000000);
  assert.equal(filled.fee, 5000);
  assert.equal(marked.marketValue, 10200000);
  assert.equal(marked.nav, 21295000);
  assert.equal(marked.pnl, 195000);
  assert.equal(marked.residual, 0);
});
test("partial active observation posts fills but retains the remaining resource", () => {
  const s = M.snapshot({ scenario: "pending", step: 9 });
  assert.equal(s.step, 7);
  assert.equal(s.quantity, 400);
  assert.equal(s.cash, 17098000);
  assert.equal(s.hold, 6003000);
  assert.equal(s.pendingQuantity, 600);
  assert.equal(s.residual, null);
});
test("confirmed partial cancellation releases only remaining hold and plans 600", () => {
  const before = M.snapshot({ scenario: "pending", step: 7 }),
    recovered = M.snapshot({ scenario: "pending", step: 8, resolved: true });
  assert.equal(recovered.cash, before.cash);
  assert.equal(recovered.quantity, 400);
  assert.equal(recovered.hold, 0);
  assert.equal(recovered.residual, 600);
  assert.equal(recovered.buyCalls, 1);
});
test("rejected sell does not borrow reserve cash for a buy", () => {
  const s = M.snapshot({ scenario: "sell-rejected", step: 9 });
  assert.equal(s.step, 5);
  assert.equal(s.cash, 0);
  assert.equal(s.marketValue, 21100000);
  assert.equal(s.reserve, 1900000000);
  assert.equal(s.sellCalls, 1);
  assert.equal(s.buyCalls, 0);
});
test("server outage lets the local fill occur but cannot claim server close", () => {
  const down = M.snapshot({ scenario: "server-offline", step: 9 });
  assert.equal(down.step, 6);
  assert.equal(down.quantity, 1000);
  assert.ok(down.outbox > 0);
  assert.match(down.bookStatus, /server 未入账/);
  const recovered = M.snapshot({
    scenario: "server-offline",
    step: 9,
    resolved: true,
  });
  assert.equal(recovered.outbox, 0);
  assert.equal(recovered.buyCalls, 1);
});
test("lost grant response reserves once, and recovery does not repeat allocation", () => {
  const before = M.snapshot({ scenario: "receipt-lost", step: 4 }),
    after = M.snapshot({ scenario: "receipt-lost", step: 4, resolved: true });
  assert.equal(before.hold, after.hold);
  assert.equal(before.nav, after.nav);
  assert.equal(after.buyCalls, 0);
});
test("returned grant cannot be used for a later submission", () => {
  const s = M.snapshot({ scenario: "grant-recall", step: 9, resolved: true });
  assert.equal(s.step, 4);
  assert.equal(s.hold, 0);
  assert.equal(s.buyCalls, 0);
  assert.match(s.grantStatus, /原包不可再提交/);
});
test("fee estimate holds extra reserve; final fee posts only the difference", () => {
  const provisional = M.snapshot({ scenario: "fees", step: 9 }),
    final = M.snapshot({ scenario: "fees", step: 9, resolved: true });
  assert.equal(provisional.hold, 1000);
  assert.equal(provisional.nav, 21295000);
  assert.equal(final.hold, 0);
  assert.equal(final.fee, 5030);
  assert.equal(final.nav, 21294970);
  assert.equal(provisional.nav - final.nav, 30);
});
test("unknown account gap is not presented as a verified balance", () => {
  const s = M.snapshot({ scenario: "cash-gap", step: 9 });
  assert.equal(s.uncertain, true);
  assert.equal(s.buyCalls, 0);
  assert.equal(s.canAdvance, false);
});
test("notification failure leaves financial facts unchanged", () => {
  const base = M.snapshot({ step: 9 }),
    failed = M.snapshot({ scenario: "notify-failed", step: 9 });
  for (const key of ["cash", "hold", "marketValue", "nav", "pnl", "buyCalls"])
    assert.equal(failed[key], base[key]);
});
test("bad hashes and unsupported shadow scenarios cannot produce invalid state", () => {
  assert.equal(
    M.snapshot({ mode: "evil", scenario: "__proto__", step: Infinity }).mode,
    "live",
  );
  assert.equal(
    M.snapshot({ mode: "shadow", scenario: "server-offline" }).scenario,
    "normal",
  );
  assert.equal(M.snapshot({ step: -100 }).step, 0);
});
test("market loss stays with its strategy without a reserve top-up", () => {
  const s = M.snapshot({ scenario: "drawdown", step: 9 });
  assert.equal(s.nav, 20895000);
  assert.equal(s.pnl, -205000);
  assert.equal(s.reserve, 1900000000);
  assert.equal(s.allocation, 21100000);
});
test("illustrative dividend exchanges raw marked value for cash without double counting", () => {
  const before = M.snapshot({ scenario: "dividend", step: 8 });
  const after = M.snapshot({ scenario: "dividend", step: 9 });
  assert.equal(after.dividend, 50000);
  assert.equal(after.cash - before.cash, 50000);
  assert.equal(before.marketValue - after.marketValue, 50000);
  assert.equal(after.nav, before.nav);
  assert.equal(after.allocation, before.allocation);
});
