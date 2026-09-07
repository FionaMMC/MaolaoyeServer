/* Illustrative accounting only. Integer cents, no I/O, no trading capability. */
(function (root) {
  "use strict";
  const ACCOUNT = 1921100000;
  const CAPITAL = 21100000;
  const RESERVE = ACCOUNT - CAPITAL;
  const PRICE = 10000;
  const TARGET = 1000;
  const ORDER_HOLD = 10005000;
  const MODES = ["live", "paper", "shadow"];
  const SCENARIOS = Object.freeze({
    normal: { modes: MODES },
    deposit: { modes: ["live", "paper"] },
    "server-offline": {
      modes: ["live", "paper"],
      stop: 6,
      recover: "演示 server 恢复",
    },
    partial: { modes: MODES },
    pending: {
      modes: ["live", "paper"],
      stop: 7,
      recover: "演示：确认余单已撤",
    },
    "sell-rejected": { modes: ["live", "paper"], stop: 5 },
    frozen: { modes: ["live", "paper"] },
    "grant-recall": {
      modes: ["live", "paper"],
      stop: 4,
      recover: "演示：交回未用授权",
    },
    "cash-gap": { modes: ["live", "paper"], stop: 5 },
    "missing-data": { modes: MODES, stop: 2, recover: "演示：必要数据已齐" },
    "receipt-lost": {
      modes: ["live", "paper"],
      stop: 4,
      recover: "演示：取回原回执",
    },
    fees: { modes: MODES, recover: "演示：收到正式费用" },
    drawdown: { modes: MODES },
    dividend: { modes: MODES },
    "notify-failed": { modes: MODES, recover: "演示：通知重试成功" },
  });

  function normalize(input = {}) {
    const mode = MODES.includes(input.mode) ? input.mode : "live";
    const key = Object.hasOwn(SCENARIOS, input.scenario)
      ? input.scenario
      : "normal";
    const scenario = SCENARIOS[key].modes.includes(mode) ? key : "normal";
    const resolved = Boolean(input.resolved && SCENARIOS[scenario].recover);
    const rawStep = Number(input.step);
    const requested = Number.isFinite(rawStep)
      ? Math.max(0, Math.min(9, Math.trunc(rawStep)))
      : 0;
    const constraint = SCENARIOS[scenario];
    const maxStep =
      scenario === "grant-recall"
        ? 4
        : !resolved && constraint.stop !== undefined
          ? constraint.stop
          : 9;
    return {
      mode,
      scenario,
      resolved,
      step: Math.min(requested, maxStep),
      maxStep,
    };
  }

  function snapshot(input = {}) {
    const state = normalize(input);
    const { step, scenario, resolved, mode } = state;
    let externalFlow = scenario === "deposit" && step >= 5 ? 100000000 : 0;
    let allocation = step >= 1 ? CAPITAL : 0;
    let reserve = ACCOUNT - allocation + externalFlow;
    let quantity = 0;
    let price = PRICE;
    let fee = 0;
    let hold = step >= 4 ? ORDER_HOLD : 0;
    let cash = allocation;
    let buyCalls = 0;
    let sellCalls = 0;
    let pendingQuantity = 0;
    let residual = null;
    let grantStatus =
      step < 4 ? "未发放" : mode === "shadow" ? "虚拟预留" : "已准备";
    let tradeStatus = "尚未执行";
    let bookStatus = "演示账本已解释";
    let brokerStatus = mode === "shadow" ? "不连接券商" : "演示：可连接";
    let serverStatus = "演示：可连接";
    let financialStatus = "尚未估值";
    let notificationStatus = "未触发";
    let outbox = 0;
    let dividend = 0;
    const isPartial = ["partial", "pending"].includes(scenario);

    if (step >= 5) {
      buyCalls = 1;
      pendingQuantity = TARGET;
      tradeStatus =
        mode === "shadow" ? "虚拟计划已提交" : "委托已发出 · 尚未证明成交";
      grantStatus = mode === "shadow" ? "虚拟占用中" : "执行占用中";
    }
    if (step >= 6) {
      quantity = isPartial ? 400 : TARGET;
      fee = quantity * 5; // Example: 0.05% of a ¥100/share basket. Not a broker fee schedule.
      cash -= quantity * PRICE + fee;
      pendingQuantity = TARGET - quantity;
      hold = pendingQuantity * (PRICE + 5);
      tradeStatus = pendingQuantity
        ? "已成 400 · 余 600 尚待确认"
        : "示例订单全部成交";
      grantStatus = hold ? "仍有剩余占用" : "已用金额已记账";
    }
    if (step >= 7) {
      const stillPending = scenario === "pending" && !resolved;
      if (!stillPending) {
        pendingQuantity = 0;
        hold = 0;
        tradeStatus = isPartial
          ? "已成 400 · 余 600 已撤（终态）"
          : "已全成（终态）";
        grantStatus = "剩余资源已交回";
      } else {
        tradeStatus = "已成 400 · 余 600 仍已报";
        bookStatus = "已成 400 入账 · 冲突补单等待";
      }
    }
    if (step >= 8) residual = TARGET - quantity;
    if (step >= 9) {
      price = 10200;
      financialStatus = "演示净值 · 价格与费用均为假设";
      notificationStatus = "演示：正常";
    }
    if (scenario === "drawdown" && step >= 9) price = 9800;
    if (scenario === "dividend" && step >= 9) {
      // Deliberately isolate the cash/price substitution: no tax and no other
      // price move in this teaching example. Production needs entitlement,
      // receivable, withholding, payment IDs and independent raw valuation.
      dividend = quantity * 50;
      cash += dividend;
      price = 9950;
      financialStatus =
        mode === "shadow"
          ? "模型分红已入虚拟现金 · 不冒充真实支付"
          : "示例股息到账 500 · 原价除息，不重复加收益";
    }

    if (scenario === "server-offline" && step >= 5 && !resolved) {
      serverStatus = "断线 · 本地执行不依赖它";
      outbox = step >= 6 ? 2 : 1;
      bookStatus = "仅本地事实已保存 · server 未入账";
    }
    if (scenario === "server-offline" && resolved)
      bookStatus = "演示已续传 · 原事实只记一次";
    if (scenario === "sell-rejected") {
      // Alternative opening portfolio, not a fabricated sale in the normal story.
      quantity = step >= 1 ? 2110 : 0;
      cash = 0;
      hold = 0;
      fee = 0;
      buyCalls = 0;
      sellCalls = step >= 5 ? 1 : 0;
      pendingQuantity = 0;
      tradeStatus =
        step >= 5
          ? "卖单明确拒绝 · 买单等待自己的现金"
          : "另设期初：21.1 万全部为持仓";
      grantStatus = step >= 4 ? "卖出份额已授权 · 不预支卖款" : "未发放";
    }
    if (scenario === "grant-recall" && resolved) {
      hold = 0;
      grantStatus = "未用授权已交回 · 原包不可再提交";
      bookStatus = "使用权释放 · 不是新增资本或盈利";
    }
    if (scenario === "receipt-lost" && step >= 4 && !resolved) {
      grantStatus = "server 已预留 · 客户端尚未取到原回执";
    }
    if (scenario === "receipt-lost" && resolved)
      grantStatus = step === 4 ? "同一份原授权已取回" : grantStatus;
    if (scenario === "fees" && step >= 9) {
      if (resolved) {
        cash -= 30;
        fee += 30;
        financialStatus = "演示：正式费用 50.30，追加更正 0.30";
      } else {
        hold = 1000;
        financialStatus = "暂估 · 另留 10 元费用准备金";
      }
    }
    if (scenario === "notify-failed" && step >= 9)
      notificationStatus = resolved
        ? "演示：通知重试成功"
        : "失败 · 不改变成交与账务";
    if (scenario === "missing-data" && !resolved)
      bookStatus = "新目标等必要数据 · 到账与其他策略继续";
    const uncertain = scenario === "cash-gap" && step >= 5;
    if (uncertain) grantStatus = "资源影响待核实 · 不新增交易风险";
    if (uncertain) {
      buyCalls = 0;
      pendingQuantity = 0;
      tradeStatus = "账户新增风险暂停";
      bookStatus = "提款事实保存 · 资源归属待核实";
    }

    const marketValue = quantity * price;
    const nav = cash + marketValue;
    const pnl = nav - allocation;
    const available = Math.max(0, cash - hold);
    const accountNav = reserve + nav;
    return Object.freeze({
      ...state,
      externalFlow,
      allocation,
      reserve,
      cash,
      hold,
      available,
      quantity,
      price,
      fee,
      marketValue,
      nav,
      pnl,
      accountNav,
      pendingQuantity,
      residual,
      buyCalls,
      sellCalls,
      grantStatus,
      tradeStatus,
      bookStatus,
      brokerStatus,
      serverStatus,
      financialStatus,
      notificationStatus,
      outbox,
      uncertain,
      dividend,
      canAdvance: step < state.maxStep,
      recovery: !resolved ? SCENARIOS[scenario].recover || null : null,
      // This identity checks the illustrative model only, not real reconciliation.
      conserved:
        accountNav === ACCOUNT + externalFlow + pnl &&
        available + hold === cash,
    });
  }

  const api = Object.freeze({
    snapshot,
    normalize,
    SCENARIOS,
    MODES,
    CAPITAL,
    ACCOUNT,
    RESERVE,
  });
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.HydraBlueprintModel = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
