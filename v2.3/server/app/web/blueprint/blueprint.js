(function () {
  "use strict";
  const C = JSON.parse(
    document.getElementById("blueprint-catalog").textContent,
  );
  const M = window.HydraBlueprintModel;
  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const money = (cents) =>
    "¥" +
    (cents / 100).toLocaleString("zh-CN", {
      minimumFractionDigits: cents % 100 ? 2 : 0,
      maximumFractionDigits: 2,
    });
  const signed = (cents) =>
    (cents > 0 ? "+" : cents < 0 ? "−" : "") + money(Math.abs(cents));
  const modeMap = Object.fromEntries(C.modes.map((x) => [x.id, x]));
  const scenarioMap = Object.fromEntries(C.scenarios.map((x) => [x.id, x]));
  const itemMap = Object.fromEntries(
    [...C.stages, ...C.gates, ...C.revisions].map((x) => [x.id, x]),
  );
  const KINDS = {
    keep: "保留硬边界",
    change: "改实现 / 缩范围",
    remove: "移除错误阻断",
    soften: "独立提示",
    clarify: "补清边界",
  };
  const STATUS = {
    pending: "待讨论",
    confirmed: "同意方案",
    change_required: "建议修改",
    follow_up: "需要核实",
    not_applicable: "此处不适用",
  };
  const VIEWS = ["journey", "worlds", "scenarios", "gates", "discussion"];
  const STORAGE_KEY = "hydra_blueprint_notes_v1";
  const SESSION = "three-books-blueprint-20260907-v1";
  let state = {
    ...M.normalize({ mode: "live", scenario: "normal", step: 0 }),
    view: "journey",
  };
  let timer = null;
  let toastTimer = null;
  let motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let activeItem = null;
  let dialogReturnFocus = null;
  let keyInMemory = "";
  let shared = { comments: [], decisions: [], updated_at: null };
  let sharedConnected = false;
  let storageWorks = true;
  let drafts = readDrafts();

  function readDrafts() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
        return {};
      const clean = {};
      for (const [id, row] of Object.entries(parsed)) {
        if (!itemMap[id] || !row || typeof row !== "object") continue;
        if (
          !Object.hasOwn(STATUS, row.status) ||
          typeof row.rationale !== "string"
        )
          continue;
        clean[id] = {
          status: row.status,
          rationale: row.rationale.slice(0, 4000),
          owner: String(row.owner || "").slice(0, 80),
          updated_by: String(row.updated_by || "").slice(0, 80),
          updated_at: String(row.updated_at || "").slice(0, 40),
        };
      }
      return clean;
    } catch (_) {
      storageWorks = false;
      return {};
    }
  }

  function toast(message) {
    $("toast").textContent = message;
    $("toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      $("toast").hidden = true;
    }, 4600);
  }

  function readHash() {
    const [view, query = ""] = location.hash.replace(/^#/, "").split("?");
    const q = new URLSearchParams(query);
    return {
      ...M.normalize({
        mode: q.get("mode"),
        scenario: q.get("scenario"),
        step: q.get("step"),
        resolved: q.get("resolved") === "1",
      }),
      view: VIEWS.includes(view) ? view : "journey",
    };
  }
  function writeHash() {
    const q = new URLSearchParams({
      mode: state.mode,
      scenario: state.scenario,
      step: String(state.step),
    });
    if (state.resolved) q.set("resolved", "1");
    history.replaceState(null, "", "#" + state.view + "?" + q.toString());
  }
  function stageNow() {
    const base = C.stages[state.step];
    return state.mode === "shadow" ? { ...base, ...base.shadow } : base;
  }
  function announce() {
    const stage = stageNow();
    $("player-announcement").textContent =
      `第 ${state.step + 1} 步：${stage.short}。${stage.title}`;
  }
  function setStage(step) {
    const before = state.step;
    state = { ...state, ...M.normalize({ ...state, step }) };
    renderJourney();
    writeHash();
    if (before !== state.step) announce();
  }
  function pause() {
    if (timer) clearInterval(timer);
    timer = null;
    $("play-label").textContent =
      state.step >= M.snapshot(state).maxStep ? "重播旅程" : "播放旅程";
    $("play-icon").textContent = "▶";
    $("play").setAttribute("aria-label", "播放资金旅程");
    renderMap();
  }
  function play() {
    if (timer) {
      pause();
      renderMap();
      return;
    }
    if (!M.snapshot(state).canAdvance) setStage(0);
    $("play-label").textContent = "暂停播放";
    $("play-icon").textContent = "Ⅱ";
    $("play").setAttribute("aria-label", "暂停资金旅程");
    timer = setInterval(
      () => {
        const s = M.snapshot(state);
        if (!s.canAdvance) {
          pause();
          renderMap();
          return;
        }
        setStage(s.step + 1);
        if (!M.snapshot(state).canAdvance) {
          pause();
          if (state.step < 9)
            toast("演示在需要证据的边界停下了。看下方预案，不会假装已经恢复。");
        }
      },
      Number($("speed").value),
    );
    renderMap();
  }
  function showView(view, scroll = true) {
    if (!VIEWS.includes(view)) return;
    pause();
    state.view = view;
    for (const id of VIEWS) $("view-" + id).hidden = id !== view;
    document.querySelectorAll(".section-nav [data-view]").forEach((a) => {
      a.classList.toggle("active", a.dataset.view === view);
      if (a.dataset.view === view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    if (view === "discussion") renderMinutes();
    writeHash();
    if (scroll)
      window.scrollTo({
        top: 0,
        behavior: motion.matches ? "instant" : "smooth",
      });
  }
  function chooseMode(mode) {
    pause();
    state = {
      ...state,
      ...M.normalize({ ...state, mode, step: state.step, resolved: false }),
    };
    renderJourney();
    writeHash();
  }
  function chooseScenario(id) {
    const scenario = scenarioMap[id];
    if (!scenario) return;
    pause();
    const mode = scenario.modes.includes(state.mode)
      ? state.mode
      : scenario.modes[0];
    state = {
      ...state,
      ...M.normalize({
        mode,
        scenario: id,
        step: scenario.step,
        resolved: false,
      }),
    };
    showView("journey");
    renderJourney();
    announce();
  }
  function gatePills(ids) {
    return ids
      .map(
        (id) =>
          `<button class="gate-pill" data-item="${esc(id)}" aria-label="查看 ${esc(itemMap[id].code)} ${esc(itemMap[id].name)}">${esc(itemMap[id].code)} ↗</button>`,
      )
      .join("");
  }

  const ICONS = {
    data: "M-9-7h18v14h-18z M-9-2h18 M-3-7v14 M3-7v14",
    model: "M-10 6l6-8 6 4 8-10 M-10 10h20",
    target:
      "M-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0 M-4 0a4 4 0 1 0 8 0a4 4 0 1 0-8 0 M0-12v5 M7 0h5",
    reserve: "M-10-3l10-7 10 7 M-9 8h18 M-7-1v7 M0-1v7 M7-1v7",
    ledger: "M-10-7h19v15h-19z M4-2h8v7h-8z M7 1h1",
    packet: "M-9-9h12l5 5v14h-17z M3-9v6h5 M-5 2l3 3 6-6",
    broker: "M-10-8h20v13h-20z M-4 10h8 M0 5v5 M-6-3l3 2-3 2 M1 1h5",
    facts: "M-8-8h16v18h-16z M-4-3h8 M-4 1h8 M-4 5h5",
    journal: "M-9-8h18v18h-18z M-5-3h10 M-5 1h4 M-4 5l3 3 7-7",
  };
  function renderMap() {
    const s = M.snapshot(state),
      stage = stageNow(),
      mode = modeMap[state.mode];
    const shadow = state.mode === "shadow";
    const nodes = [
      {
        id: "data",
        x: 26,
        y: 57,
        title: "四份冻结数据",
        sub: "行情 · 原价 · 行动 · 日历",
        step: 2,
        group: "DATA",
      },
      {
        id: "model",
        x: 305,
        y: 57,
        title: "策略模型",
        sub: "复权信号 · 风险与权重",
        step: 2,
        group: "RESEARCH",
      },
      {
        id: "target",
        x: 584,
        y: 57,
        title: "目标与差额计划",
        sub: "服务器计算数量与限价",
        step: 3,
        group: "DECISION",
      },
      {
        id: "reserve",
        x: 26,
        y: 213,
        title: mode.cashLabel,
        sub: mode.capitalLabel + " · 不是策略预算",
        step: 0,
        group: "CAPITAL",
      },
      {
        id: "ledger",
        x: 305,
        y: 213,
        title: "Hydra 子账本",
        sub: "只用自己的资本与盈亏",
        step: 1,
        group: "OWNERSHIP",
      },
      {
        id: "packet",
        x: 584,
        y: 213,
        title: shadow ? "虚拟执行计划" : "冻结的本地执行包",
        sub: shadow ? "没有真实下单能力" : "订单 + 自己的额度 + 回执",
        step: 4,
        group: shadow ? "MODEL ONLY" : "PERMISSION",
      },
      {
        id: "journal",
        x: 26,
        y: 369,
        title: "归属记账与结案",
        sub: "收事实 · 释放剩余 · 算净值",
        step: 7,
        group: "ACCOUNTING",
      },
      {
        id: "facts",
        x: 305,
        y: 369,
        title: shadow ? "虚拟成交事件" : "委托与成交事实",
        sub: shadow ? "来源：版本化撮合模型" : "已成多少 · 是否仍会成交",
        step: 6,
        group: "EVIDENCE",
      },
      {
        id: "broker",
        x: 584,
        y: 369,
        title: shadow
          ? "虚拟撮合器"
          : state.mode === "paper"
            ? "QMT 模拟账户"
            : "本地 MiniQMT",
        sub: shadow ? "模型假设 ≠ 真实成交" : "只执行批准包 · 不重算策略",
        step: 5,
        group: shadow ? "SERVER" : "WINDOWS",
      },
    ];
    const paths = [
      ["data-model", "M262 104H305", "data"],
      ["model-target", "M541 104H584", "data"],
      ["target-packet", "M702 151V213", "data"],
      ["account-ledger", "M262 260H305", "cash"],
      ["ledger-packet", "M541 260H584", "cash"],
      ["packet-broker", "M702 307V369", "order"],
      ["broker-facts", "M584 416H541", "order"],
      ["facts-journal", "M305 416H262", "order"],
      [
        "journal-ledger",
        "M144 369V342Q144 331 155 331H412Q423 331 423 320V307",
        "cash",
      ],
      [
        "ledger-target",
        "M423 213V187Q423 177 433 177H692Q702 177 702 167V151",
        "data",
      ],
    ];
    const activePath = state.step === 8 ? "ledger-target" : stage.path;
    const edge = paths.find((p) => p[0] === activePath);
    const blocked = !s.canAdvance && state.step < 9 && !state.resolved;
    const pathSvg = paths
      .map(
        ([id, d, type]) =>
          `<path id="edge-${id}" d="${d}" class="flow-edge ${type} ${id === activePath ? "active" : ""} ${s.outbox && id === "facts-journal" ? "disconnected" : ""}" marker-end="url(#arrow-${id === activePath ? "active" : "base"})"/>`,
      )
      .join("");
    const nodeSvg = nodes
      .map((n) => {
        const active = stage.node === n.id;
        const cls = `map-node ${active ? "active" : ""} ${n.step < state.step ? "complete" : ""} ${active && blocked ? (scenarioMap[state.scenario].tone === "block" ? "block" : "wait") : ""}`;
        return `<g class="${cls}" role="button" tabindex="0" data-step="${n.step}" aria-label="查看${esc(n.title)}：第 ${n.step + 1} 步"><rect class="node-box" x="${n.x}" y="${n.y}" width="236" height="94" rx="11"/><rect class="node-icon-bg" x="${n.x + 14}" y="${n.y + 15}" width="29" height="29" rx="7"/><path class="node-icon" d="${ICONS[n.id]}" transform="translate(${n.x + 28.5} ${n.y + 29.5}) scale(.8)"/><text class="node-number" x="${n.x + 54}" y="${n.y + 31}">${esc(n.group)}</text><text class="node-title" x="${n.x + 15}" y="${n.y + 61}">${esc(n.title)}</text><text class="node-sub" x="${n.x + 15}" y="${n.y + 79}">${esc(n.sub)}</text>${active ? `<circle cx="${n.x + 219}" cy="${n.y + 20}" r="3.5" class="map-active-dot"/>` : ""}</g>`;
      })
      .join("");
    const particle =
      timer && !motion.matches && edge && !blocked
        ? `<circle r="4" fill="${esc(mode.color)}"><animateMotion dur="1.7s" repeatCount="indefinite" path="${edge[1]}"/></circle>`
        : "";
    $("flow-map").innerHTML =
      `<svg viewBox="0 0 850 497" role="group" aria-label="${esc(mode.name)}的数据、资金、授权与事实流"><defs><marker id="arrow-base" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0 0L6 3L0 6" fill="none" stroke="#c2cdbb"/></marker><marker id="arrow-active" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0 0L6 3L0 6" fill="none" stroke="${esc(mode.color)}"/></marker></defs><text class="map-layer-label" x="27" y="34">01 / 数据变成决策</text><text class="map-layer-label" x="27" y="190">02 / 资金有归属，使用有授权</text><text class="map-layer-label" x="27" y="348">03 / 执行留下事实，事实回到账本</text>${pathSvg}${nodeSvg}${particle}${s.outbox ? '<text x="271" y="476" class="map-layer-label" style="fill:#ae834a">SERVER 断线：事实先存本地，恢复后续传</text>' : ""}</svg>`;
    // A readable phone layout, not a desktop diagram shrunk to six-pixel text.
    const lanes = [
      ["数据变成决策", ["data", "model", "target"]],
      ["资金有归属，使用有授权", ["reserve", "ledger", "packet"]],
      ["执行留下事实，事实回到账本", ["broker", "facts", "journal"]],
    ];
    $("flow-map").insertAdjacentHTML(
      "beforeend",
      `<div class="compact-map" aria-label="手机流程图">${lanes
        .map(
          ([label, ids], i) =>
            `<div class="compact-lane"><p>0${i + 1} / ${label}</p><div>${ids
              .map((id) => {
                const n = nodes.find((x) => x.id === id);
                return `<button class="map-mini-node ${stage.node === id ? "active" : ""}" data-step="${n.step}" aria-label="查看${esc(n.title)}：第 ${n.step + 1} 步"><span>${esc(n.group)}</span><strong>${esc(n.title)}</strong></button>`;
              })
              .join("")}</div></div>`,
        )
        .join(
          "",
        )}<p class="compact-foot">目标 + 自有额度 → 执行包<br>成交记账 → 服务器重算剩余差额${s.outbox ? "<br>断线时：事实先存本地，恢复后续传" : ""}</p></div>`,
    );
  }

  function renderJourney() {
    const s = M.snapshot(state),
      stage = stageNow(),
      mode = modeMap[state.mode];
    document.documentElement.style.setProperty("--mode", mode.color);
    document
      .querySelectorAll("[data-mode]")
      .forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.mode === state.mode)),
      );
    $("scenario-select").innerHTML = C.scenarios
      .filter((x) => x.modes.includes(state.mode))
      .map(
        (x) =>
          `<option value="${x.id}" ${x.id === state.scenario ? "selected" : ""}>${esc(x.name)}</option>`,
      )
      .join("");
    $("mode-explanation").innerHTML =
      `<b>${esc(mode.tag)}</b><span>／ ${esc(mode.boundary)}</span>`;
    $("phase-label").textContent = stage.phase;
    $("flow-stage-title").textContent = stage.short;
    $("step-counter").innerHTML =
      String(state.step + 1).padStart(2, "0") + " <span>/ 10</span>";
    for (const [id, key] of [
      ["step-title", "title"],
      ["step-time", "time"],
      ["step-description", "description"],
      ["step-owner", "owner"],
      ["step-auto", "automatic"],
      ["step-input", "inputs"],
      ["step-output", "outputs"],
      ["step-failure", "failure"],
    ])
      $(id).textContent = stage[key];
    const stageGates =
      state.mode === "shadow" && state.step === 5
        ? [...new Set([...stage.gates, "g02"])]
        : stage.gates;
    $("stage-gates").innerHTML = gatePills(
      stageGates.filter((id) => itemMap[id].modes.includes(state.mode)),
    );
    $("previous").disabled = state.step === 0;
    $("next").disabled = !s.canAdvance;
    if (!timer)
      $("play-label").textContent = !s.canAdvance ? "重播旅程" : "播放旅程";
    $("timeline").innerHTML = C.stages
      .map(
        (x, i) =>
          `<button class="stage-dot ${i === state.step ? "active" : ""} ${i < state.step ? "done" : ""}" data-step="${i}" ${i > s.maxStep ? "disabled" : ""} ${i === state.step ? 'aria-current="step"' : ""} aria-label="第 ${i + 1} 步 ${esc(state.mode === "shadow" ? x.shadow.short || x.short : x.short)}"><b>${i < state.step ? "✓" : String(i + 1).padStart(2, "0")}</b><span>${esc(state.mode === "shadow" ? x.shadow.short || x.short : x.short)}</span></button>`,
      )
      .join("");
    renderMap();
    renderMoney(s, stage);
    renderScenarioCallout(s);
    renderQuick();
  }

  function renderMoney(s, stage) {
    const uncertainValue = (value) => (s.uncertain ? "待核实" : money(value));
    $("reserve-label").textContent = modeMap[state.mode].capitalLabel;
    $("reserve-value").textContent = uncertainValue(s.reserve);
    for (const [id, value] of [
      ["cash-value", s.cash],
      ["position-value", s.marketValue],
      ["nav-value", s.nav],
      ["free-value", s.available],
      ["held-value", s.hold],
      ["capital-value", s.allocation],
      ["fee-value", s.fee],
    ])
      $(id).textContent = uncertainValue(value);
    $("pnl-value").textContent = s.uncertain ? "不可确认" : signed(s.pnl);
    $("pnl-value").classList.toggle("negative", s.pnl < 0);
    const denominator = s.cash || 1;
    $("free-bar").style.width =
      (s.uncertain ? 0 : (100 * s.available) / denominator) + "%";
    $("held-bar").style.width =
      (s.uncertain ? 0 : (100 * s.hold) / denominator) + "%";
    $("wallet-quality").textContent = s.uncertain
      ? "资源归属待核实"
      : s.outbox
        ? "按本地已知事实演示 · 未同步 server"
        : state.mode === "shadow"
          ? "虚拟归属账"
          : "演示归属账";
    for (const [id, value] of [
      ["step-cash", s.cash],
      ["step-held", s.hold],
      ["step-nav", s.nav],
    ])
      $(id).textContent = uncertainValue(value);
    $("step-money-note").textContent = s.uncertain
      ? "未知差额先留证，不把旧余额当成可花的钱。"
      : s.outbox
        ? "本地已知事实的示例，server 尚未记账。"
        : s.hold
          ? "占用只是承诺用途，不再扣减一次净资产。"
          : "储备不归 Hydra；收益与亏损留在原策略。";
    $("money-caption").textContent =
      state.scenario === "sell-rejected"
        ? "另设期初：21.1 万全是持仓，现金为 0"
        : "可复算教学示例 · 非真实余额";
    $("money-explanation").textContent = s.uncertain
      ? "已知真实缺口的来源尚未查清，不能把最后快照当成当前可花的钱。"
      : `账面现金 = 未承诺 + 占用；净资产 = 现金 + 持仓。${s.hold ? "占用只限制使用，不再扣减一次净值。" : "储备不进入策略净资产。"}`;
    $("conservation-label").textContent = s.uncertain
      ? "◇ 事实已保留，但没有足够证据宣布账户对平。"
      : `✓ 示例资产守恒：${money(s.reserve)} 储备 + ${money(s.nav)} 策略 = ${money(s.accountNav)}；并非真实对账通过。`;
    const runtime = [
      [
        "交易 / 执行",
        s.tradeStatus,
        s.pendingQuantity > 0 ||
          ["sell-rejected", "cash-gap"].includes(state.scenario),
      ],
      ["事实 / 账务", s.bookStatus, Boolean(s.outbox) || s.uncertain],
      [
        "资源授权",
        s.grantStatus,
        ["receipt-lost", "grant-recall"].includes(state.scenario) &&
          !state.resolved,
      ],
    ];
    if (state.step >= 9)
      runtime[2] = [
        "财务 / 通知",
        state.scenario === "notify-failed"
          ? s.notificationStatus
          : s.financialStatus,
        ["fees", "notify-failed"].includes(state.scenario) && !state.resolved,
      ];
    $("runtime-status").innerHTML = runtime
      .map(
        ([label, value, wait]) =>
          `<div class="runtime-item ${wait ? "wait" : ""}"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`,
      )
      .join("");
    // Stage explanations are the general flow. Scenario facts override narration,
    // never silently reuse the normal all-filled financial claims.
    if (state.scenario !== "normal") {
      const scenario = scenarioMap[state.scenario];
      $("step-description").textContent = scenario.cause;
      if (state.step >= 6 && ["partial", "pending"].includes(state.scenario))
        $("step-title").textContent =
          state.step >= 8
            ? "目标未变，剩余仍是 600 份"
            : "已成 400 份，不等于余下的也结束了";
      if (state.scenario === "sell-rejected")
        $("step-title").textContent = "账户里的闲钱，不是 Hydra 的卖款";
      if (state.scenario === "fees" && state.step === 9)
        $("step-title").textContent = state.resolved
          ? "只追加更正 0.30 元，不重新交易"
          : "先留准备金，净值明确标暂估";
      if (state.scenario === "drawdown" && state.step === 9)
        $("step-title").textContent = "亏了 2,050 元，也不动别人的钱";
      if (state.scenario === "dividend" && state.step === 9)
        $("step-title").textContent = "分红 500 元，不能把净值再抬高一次";
    }
  }

  function renderScenarioCallout(s) {
    const box = $("scenario-callout");
    box.hidden = state.scenario === "normal";
    if (box.hidden) return;
    const c = scenarioMap[state.scenario];
    const resolved = state.resolved;
    let recoveryText = c.recovery;
    if (resolved) {
      const recovery = {
        "server-offline":
          "已演示恢复连接：本地事实按原编号续传；不再次调用下单。",
        pending:
          "已演示获得余单已撤的终态证据：600 份剩余占用释放；下一次差额仍是 600 份。",
        "grant-recall":
          "已演示原执行器交回未用授权：现金不变，占用释放。原包不能继续提交。",
        "missing-data":
          "已演示补齐同一任务依赖的数据：现在可继续计算，不需要重记入金。",
        "receipt-lost": "已演示取回原回执：仍只有同一份授权、同一次预留。",
        fees: "已演示正式费用为 50.30 元：追加费用 0.30，释放多余准备金；未重复扣除 50 元。",
        "notify-failed": "已演示通知恢复；交易和账务没有重做。",
      };
      recoveryText = recovery[state.scenario] || recoveryText;
    }
    box.className = `scenario-callout ${resolved ? "ok" : c.tone}`;
    const recoveryButton = s.recovery
      ? `<button class="primary-button" id="recover-scenario">${esc(s.recovery)} →</button>`
      : resolved && s.canAdvance
        ? '<button class="primary-button" id="continue-recovery">继续下一步 →</button>'
        : '<button class="outline-button" id="back-normal">回到正常旅程</button>';
    const owner =
      state.mode === "shadow" && ["partial", "fees"].includes(c.id)
        ? "服务器虚拟撮合 / 账务工作流；不用柜台回报冒充模型事件"
        : c.owner;
    box.innerHTML = `<div class="callout-heading"><h3>${esc(c.question)}</h3><span class="outcome ${resolved ? "ok" : c.tone}">${resolved ? "已演示恢复证据" : esc(c.outcome)}</span></div><p class="callout-cause">${esc(c.cause)}</p><div class="callout-grid"><div><h4>只暂停什么</h4><p>${esc(c.stops)}</p></div><div><h4>什么必须继续</h4><p>${esc(c.continues)}</p></div><div><h4>怎样恢复</h4><p>${esc(recoveryText)}</p></div><div><h4>谁接手 · 哪些闸门</h4><p>${esc(owner)}</p><div style="margin-top:7px;display:flex;gap:5px;flex-wrap:wrap">${gatePills(c.gates.filter((id) => itemMap[id].modes.includes(state.mode)))}</div></div></div><div class="callout-action"><p>${resolved ? "这是人为注入的教学证据，不是检测到真实环境已恢复。" : "恢复演示只注入明确的教学证据，不会重连或操作真实账户。"}</p>${recoveryButton}</div>`;
  }
  function renderQuick() {
    const ids =
      state.mode === "shadow"
        ? ["partial", "missing-data", "fees", "notify-failed"]
        : ["deposit", "server-offline", "pending", "sell-rejected"];
    $("quick-grid").innerHTML = ids
      .map((id, i) => {
        const c = scenarioMap[id];
        return `<button class="quick-card" data-scenario="${id}"><span>WHAT IF / 0${i + 1}</span><strong>${esc(c.name)}</strong><small>${esc(c.short)}</small><i>↗</i></button>`;
      })
      .join("");
  }

  function renderWorlds() {
    $("world-cards").innerHTML = ["shadow", "paper", "live"]
      .map((id, i) => {
        const m = modeMap[id];
        return `<article class="world-card" style="--world:${m.color}"><span class="world-index">0${i + 1}</span><h2>${esc(m.name)}</h2><span class="world-tag">${esc(m.tag)}</span><dl><div><dt>事实来自哪里</dt><dd>${esc(m.source)}</dd></div><div><dt>谁来执行</dt><dd>${esc(m.executor)}</dd></div><div><dt>用什么作为证据</dt><dd>${esc(m.proof)}</dd></div></dl><p>${esc(m.difference)}</p><button class="inline-link" data-explore-mode="${id}">播放这个盘的旅程 →</button></article>`;
      })
      .join("");
    const components = [
      {
        name: "服务器 · 业务核心",
        tag: "决定做什么，记录归谁",
        modules: [
          ["数据与策略", "按批准的输入计算目标，不在 HTTP 请求里等模型跑完。"],
          ["资金与交易", "划拨、预留、目标转订单，基于真实持仓补差额。"],
          ["账务与估值", "接收事实、归属分录、结案、净值及费用更正。"],
        ],
        no: "不直接调用真实券商下单。",
      },
      {
        name: "Windows · 执行器",
        tag: "只按批准的包执行",
        modules: [
          ["本地持久包", "晚间存好订单、额度和原回执，早上不依赖 server。"],
          ["单账户协调器", "短暂认领资源；等待不霸占通道；撤单/恢复优先。"],
          ["采集与续传", "把委托和成交留证，server 恢复后可靠回传。"],
        ],
        no: "不计算新权重、不改补单数量、不扩大资本。",
      },
      {
        name: "API 与 Dashboard",
        tag: "接得住信息，也说得清状态",
        modules: [
          ["短请求与查询", "快速返回已收到、待核实或可查的任务编号。"],
          ["工作流可见性", "显示哪一步等待谁、下一次核查与升级责任人。"],
          ["讨论与报告", "展示证据、质量和规则；通知故障独立处理。"],
        ],
        no: "本页是讲解层，不是新的交易控制面。",
      },
    ];
    $("ownership-map").innerHTML = components
      .map(
        (c) =>
          `<article class="ownership-column"><h3>${esc(c.name)}</h3><p>${esc(c.tag)}</p>${c.modules.map(([name, body]) => `<div class="ownership-module"><b>${esc(name)}</b><p>${esc(body)}</p></div>`).join("")}<p class="no-task">边界：${esc(c.no)}</p></article>`,
      )
      .join("");
    const day = [
      ["数据就绪", "计算目标", "只检查实际依赖"],
      ["晚间准备", "领取执行包", "核对 + 资源预留"],
      ["下一交易日", "离线执行", "自己的额度 ∩ QMT 可用"],
      ["窗口内", "撤回剩余", "请求成功 ≠ 已撤"],
      ["终态到来", "结案补差", "已知事实先记账"],
      ["每个估值日", "净值与复盘", "收益留在原策略"],
    ];
    $("day-flow").innerHTML = day
      .map(
        ([a, b, c], i) =>
          `<div class="day-step"><span>0${i + 1} / ${esc(a)}</span><strong>${esc(b)}</strong><p>${esc(c)}</p></div>`,
      )
      .join("");
  }
  function renderScenarios(category = "all") {
    document
      .querySelectorAll("[data-category]")
      .forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.category === category)),
      );
    const list = C.scenarios.filter(
      (c) =>
        c.id !== "normal" && (category === "all" || c.category === category),
    );
    $("scenario-grid").innerHTML = list
      .map(
        (c, i) =>
          `<article class="scenario-card"><div class="scenario-card-head"><span>SCENARIO ${String(i + 1).padStart(2, "0")} · ${c.modes.map((x) => modeMap[x].short).join(" / ")}</span><span class="outcome ${c.tone}">${esc(c.outcome)}</span></div><h2>${esc(c.name)}</h2><p class="question">${esc(c.question)}</p><dl><div><dt>只暂停什么</dt><dd>${esc(c.stops)}</dd></div><div class="continue"><dt>必须继续什么</dt><dd>${esc(c.continues)}</dd></div><div><dt>恢复的证据与负责人</dt><dd>${esc(c.recovery)}<br>${esc(c.owner)}</dd></div></dl><div class="scenario-card-bottom"><div>${gatePills(c.gates)}</div><button class="primary-button" data-scenario="${c.id}">放进旅程里演示 →</button></div></article>`,
      )
      .join("");
  }
  function renderGates() {
    const query = $("gate-search").value.trim().toLowerCase(),
      kind = $("gate-kind").value,
      mode = $("gate-mode").value;
    const gates = C.gates.filter(
      (g) =>
        (kind === "all" || g.kind === kind) &&
        (mode === "all" || g.modes.includes(mode)) &&
        (!query ||
          [
            g.code,
            g.name,
            g.phase,
            g.threat,
            g.rule,
            g.scope,
            g.continues,
            g.recovery,
            g.current,
          ]
            .join(" ")
            .toLowerCase()
            .includes(query)),
    );
    $("gate-count").textContent =
      `显示 ${gates.length} / 26 项 · 每项可展开预案与讨论 · 状态来自设计审阅，不是实时检测`;
    $("gate-grid").innerHTML =
      gates
        .map(
          (g) =>
            `<button class="gate-card" data-item="${g.id}"><div class="gate-card-top"><span class="gate-code">${g.code} · ${esc(g.phase)}</span><span class="kind-chip ${g.kind}">${KINDS[g.kind]}</span></div><h3>${esc(g.name)}</h3><p>${esc(g.rule)}</p><div class="gate-scope">只影响：${esc(g.scope)}</div><div class="gate-card-bottom"><span>${g.modes.map((x) => modeMap[x].short).join(" / ")}</span><span>预案与讨论 ↗</span></div></button>`,
        )
        .join("") ||
      '<div class="empty-state">没有匹配的闸门。试试“入金”“终态”或清空筛选。</div>';
  }
  function renderRevisions() {
    $("revision-grid").innerHTML = C.revisions
      .map(
        (r, i) =>
          `<article class="revision-card"><span class="revision-label">0${i + 1} / ${esc(r.label)}</span><h2>${esc(r.name)}</h2><dl><dt>原来容易误解的地方</dt><dd>${esc(r.before)}</dd><dt>本次补清后的规则</dt><dd>${esc(r.after)}</dd><dt>我们仍然接受的取舍</dt><dd>${esc(r.tradeoff)}</dd></dl><p class="question-box">一起定一件事：${esc(r.question)}</p><button class="inline-link" data-item="${r.id}">记录我们对这项规则的意见 →</button></article>`,
      )
      .join("");
  }

  // Discussion/dialog and event bindings follow. They only write review notes.
  function openDialog(title, kicker, body) {
    pause();
    dialogReturnFocus = document.activeElement;
    $("dialog-title").textContent = title;
    $("dialog-kicker").textContent = kicker;
    $("dialog-body").innerHTML = body;
    if (!$("detail-dialog").open) $("detail-dialog").showModal();
  }
  function closeDialog() {
    $("detail-dialog").close();
    activeItem = null;
    if (dialogReturnFocus?.isConnected) dialogReturnFocus.focus();
  }
  function decisionFor(id) {
    return (
      shared.decisions.find((x) => x.item_id === id) ||
      drafts[id] || {
        status: "pending",
        rationale: "",
        owner: "",
        updated_by: "",
      }
    );
  }
  function noteForm(id) {
    const d = decisionFor(id),
      comments = shared.comments.filter((x) => x.item_id === id);
    return `<form class="note-form" id="note-form" data-item-id="${id}"><h3>记录设计意见</h3><p class="draft-label">${sharedConnected ? "连接到独立的新架构讨论；保存只写批注。" : "未连接共享：保存为本浏览器草稿，可导出交给同伴。"}</p><div class="form-row"><label>记录人<input name="updated_by" id="note-author" maxlength="80" required value="${esc(d.updated_by)}" autocomplete="name"></label><label>讨论状态<select name="status" id="note-status">${Object.entries(
      STATUS,
    )
      .map(
        ([k, v]) =>
          `<option value="${k}" ${d.status === k ? "selected" : ""}>${v}</option>`,
      )
      .join(
        "",
      )}</select></label></div><label>需要谁确认 / 跟进<input name="owner" id="note-owner" maxlength="80" value="${esc(d.owner)}" placeholder="例如：策略负责人 / Windows 运维"></label><label>规则、理由或还需要什么证据<textarea name="rationale" id="note-rationale" maxlength="4000" required placeholder="例如：到账可自动归储备；只有新增资本划拨需要我们批准。">${esc(d.rationale)}</textarea></label><div class="note-form-actions"><p>“同意方案”不是交易审批，不改变生产开关。</p><button type="submit" id="save-note" class="primary-button">${sharedConnected ? "保存共享意见" : "保存本地草稿"}</button></div></form><div class="comments-list">${comments.map((c) => `<div class="shared-comment"><span>${esc(c.author)} · ${esc(c.created_at)}</span><p>${esc(c.body)}</p></div>`).join("")}</div>`;
  }
  function openItem(id) {
    const item = itemMap[id];
    if (!item) return;
    activeItem = id;
    let title = item.name || item.title,
      kicker = "设计讨论 · 不改变真实资金",
      body = "";
    if (id.startsWith("g")) {
      kicker = `${item.code} / ${item.phase} · ${KINDS[item.kind]}`;
      body = `<p class="detail-lead">${esc(item.rule)}</p><div class="detail-grid"><div class="detail-cell stop"><small>在防什么</small><p>${esc(item.threat)}</p></div><div class="detail-cell stop"><small>只限制哪里</small><p>${esc(item.scope)}</p></div><div class="detail-cell"><small>什么必须继续</small><p>${esc(item.continues)}</p></div><div class="detail-cell"><small>恢复所需动作与证据</small><p>${esc(item.recovery)}</p></div></div><p class="evidence-label">当前实现与差距：${esc(item.current)}<br>证据类型：${esc(item.evidence)} · 审阅基线 2026.09.07，不代表现网已修复。<br>对应设计规范：第 ${item.section} 节 · 适用 ${item.modes.map((m) => modeMap[m].name).join(" / ")}</p>`;
    } else if (id.startsWith("revision")) {
      body = `<p class="detail-lead">${esc(item.question)}</p><div class="detail-grid"><div class="detail-cell stop"><small>原先的歧义</small><p>${esc(item.before)}</p></div><div class="detail-cell"><small>本次修订</small><p>${esc(item.after)}</p></div></div><p class="evidence-label">仍需接受的取舍：${esc(item.tradeoff)}<br>这是架构设计修订，不是生产代码修复结果。</p>`;
    } else {
      const s = state.mode === "shadow" ? { ...item, ...item.shadow } : item;
      title = s.title;
      body = `<p class="detail-lead">${esc(s.description)}</p><div class="detail-grid"><div class="detail-cell"><small>谁负责 / 如何自动运行</small><p>${esc(s.owner)}<br>${esc(s.automatic)}</p></div><div class="detail-cell stop"><small>出问题时</small><p>${esc(s.failure)}</p></div></div><p class="evidence-label">${esc(s.money)}<br>讲解模式：${esc(modeMap[state.mode].name)} · 拟议规则，不是实际状态。</p>`;
    }
    openDialog(title, kicker, body + noteForm(id));
  }
  function renderMinutes() {
    $("notes-status").textContent = sharedConnected
      ? "共享讨论已连接 · 本地草稿不会自动上传 · 仅设计意见"
      : storageWorks
        ? "讨论草稿只在本浏览器保存 · 尚未共享"
        : "浏览器存储不可用 · 可填写后导出，不能声称草稿已持久保存";
    $("connect-notes").textContent = sharedConnected
      ? "刷新 / 断开共享"
      : "连接共享讨论";
    const rows = new Map(
      Object.entries(drafts).map(([id, row]) => [
        id,
        { ...row, item_id: id, local: true },
      ]),
    );
    shared.decisions.forEach((d) =>
      rows.set(d.item_id, { ...d, local: false }),
    );
    $("minutes").innerHTML =
      Array.from(rows.values())
        .map((d) => {
          const item = itemMap[d.item_id];
          if (!item) return "";
          return `<article class="minute"><header><h3>${esc(item.name || item.title)}</h3><span class="kind-chip">${esc(STATUS[d.status] || "待核实")}</span></header><p>${esc(d.rationale)}</p><small>${esc(d.updated_by)} · ${esc(d.owner || "未指定跟进人")} · ${d.local ? "仅本地草稿" : "共享设计意见"}</small><div><button class="small-text" data-item="${d.item_id}">继续讨论 ↗</button></div></article>`;
        })
        .join("") ||
      '<div class="empty-state">还没有讨论意见。可以从一项闸门，或上面的一条取舍开始。</div>';
  }
  async function notesApi(suffix, options = {}) {
    if (!keyInMemory) throw new Error("请先连接共享讨论凭据");
    const allowed =
      suffix === "/session" ||
      suffix === "/comments" ||
      /^\/decisions\/(?:g\d{2}|stage-\d{2}|revision-\d{2})$/.test(suffix);
    if (!allowed) throw new Error("不是允许的讨论接口");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 6500);
    try {
      const response = await fetch("/admin/architecture-blueprint" + suffix, {
        ...options,
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        signal: controller.signal,
        headers: {
          Authorization: "Bearer " + keyInMemory,
          ...(options.body ? { "Content-Type": "application/json" } : {}),
        },
      });
      const body = await response.json();
      if (!response.ok || body.code !== 0)
        throw new Error(body.message || "共享讨论请求失败");
      return body.data;
    } finally {
      clearTimeout(timeout);
    }
  }
  async function refreshNotes() {
    const snap = await notesApi("/session");
    if (
      snap.session_id !== SESSION ||
      !Array.isArray(snap.decisions) ||
      !Array.isArray(snap.comments)
    )
      throw new Error("共享讨论返回了不匹配的会话");
    shared = snap;
    sharedConnected = true;
    renderMinutes();
  }
  function connectDialog() {
    activeItem = null;
    openDialog(
      "只连接共享讨论",
      "REVIEW NOTES · 非交易权限",
      `<p class="detail-lead">使用现有管理/审阅凭据。实盘下单专用 key 没有本页批注权限，不需要扩大它的白名单。凭据仅留在当前页面内存，不写入浏览器存储、链接或导出文件。</p><form id="connect-form" class="connect-form"><label>管理 / 审阅 API Key<input id="notes-key" type="password" required autocomplete="off" placeholder="只用于认证批注接口"></label><div class="note-form-actions"><button type="button" class="outline-button" id="disconnect-notes">断开并清除内存凭据</button><button type="submit" class="primary-button" id="connect-submit">连接 / 刷新讨论</button></div></form><p class="evidence-label">默认阅读和所有情景演示均不需要 key；连接后只在你明确保存时写会议意见，不调用资金、订单、撤单或策略接口。本地草稿不会被自动上传。</p>`,
    );
  }
  async function saveNote(form) {
    const id = form.dataset.itemId;
    if (!itemMap[id]) return;
    const payload = {
      status: $("note-status").value,
      updated_by: $("note-author").value.trim(),
      owner: $("note-owner").value.trim(),
      rationale: $("note-rationale").value.trim(),
    };
    if (!payload.updated_by || !payload.rationale) {
      toast("请填写记录人和讨论理由。");
      return;
    }
    const button = $("save-note");
    button.disabled = true;
    try {
      if (sharedConnected) {
        const row = await notesApi("/decisions/" + id, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
        // The write receipt is success evidence; a later refresh failure must
        // not misreport the already-saved decision as an unsuccessful write.
        if (row.item_id !== id)
          throw new Error("保存回执不匹配，请刷新核实原请求");
        shared.decisions = shared.decisions
          .filter((x) => x.item_id !== id)
          .concat(row);
        renderMinutes();
        toast("共享设计意见已保存。没有改变交易规则或开关。");
      } else {
        const next = {
          ...drafts,
          [id]: { ...payload, updated_at: new Date().toISOString() },
        };
        localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
        drafts = next;
        storageWorks = true;
        renderMinutes();
        toast("草稿已保存在本浏览器，尚未共享；可以导出给同伴。");
      }
    } catch (error) {
      toast(
        (error.name === "AbortError"
          ? "保存结果暂不确定，可刷新核实；原稿仍在。"
          : error.message) + " 输入内容已保留。",
      );
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }
  function showMath() {
    activeItem = null;
    const s = M.snapshot(state);
    if (s.uncertain) {
      openDialog(
        "未知差额不编一个数字填平",
        "示例 · 不代表账户已对平",
        '<p class="detail-lead">这个情景缺少足够证据确认提款归属与当前资源，所以不展示“精确可用余额”。需要原始资金流水、委托/成交覆盖及账本归属，才能确定影响范围；事实接收与核对继续。</p>',
      );
      return;
    }
    const lines = [
      ["外部资本起点", money(M.ACCOUNT)],
      ["本情景新增储备到账", money(s.externalFlow)],
      ["储备归属", money(s.reserve)],
      ["Hydra 明确划拨资本", money(s.allocation)],
      ["Hydra 账面现金", money(s.cash)],
      ["其中：尚未承诺", money(s.available)],
      ["其中：已占用 / 准备金", money(s.hold)],
      ["本情景归属股息（非资本划拨）", money(s.dividend)],
      [
        "归属份额 × 示例价格",
        `${s.quantity.toLocaleString()} × ${money(s.price)} = ${money(s.marketValue)}`,
      ],
      ["Hydra 净资产 = 现金 + 市值", money(s.nav)],
      ["投资损益 = 净资产 − 净资本划拨", signed(s.pnl)],
      ["储备 + Hydra = 示例总资产", money(s.accountNav)],
    ];
    openDialog(
      "钱怎样对得上？",
      "INTEGER CENTS · 教学模型，非生产验收",
      `<p class="detail-lead">所有计算使用整数分。预留是现金内部的占用状态，不再从净资产扣一次。这个示例没有未结应收应付；真实系统还需按同一口径计入它们。</p><div class="math-rows">${lines.map(([a, b]) => `<div class="math-row"><span>${esc(a)}</span><strong>${esc(b)}</strong></div>`).join("")}</div><p class="evidence-label">示例目标：1,000 份 × 100 元，费用按教学假设 0.05% 计算；当前示例估值单价 ${money(s.price)}。它不是实际 ETF 价格、正式 Hydra 权重或券商费率。当前 BUY 调用演示次数：${s.buyCalls}；SELL：${s.sellCalls}。${s.residual === null ? "尚不计算可执行补单。" : "尚余 " + s.residual + " 份目标差额；差额不等于已获准下单。"}</p>`,
    );
  }
  function exportNotes() {
    const s = M.snapshot(state),
      c = scenarioMap[state.scenario];
    const lines = [
      `# Hydra 三盘新架构 · 讲解与讨论纪要`,
      "",
      `设计版本：${C.meta.version} / ${C.meta.date}`,
      `性质：拟议规则与教学示例；不是部署证明或交易批准。`,
      `当前情景：${modeMap[state.mode].name} / ${c.name} / 第 ${state.step + 1} 步${state.resolved ? " / 已注入演示恢复证据" : ""}`,
      `讨论来源：${sharedConnected ? "已连接共享讨论；另保留本地草稿标记" : "仅本浏览器草稿，尚未共享"}`,
      "",
      "## 三盘的边界",
      "",
    ];
    C.modes.forEach((m) =>
      lines.push(
        `### ${m.name}`,
        "",
        `${m.source}；执行：${m.executor}。${m.boundary}`,
        m.difference,
        "",
      ),
    );
    lines.push("## 一笔钱的旅程", "");
    C.stages.forEach((r, i) => {
      const t = state.mode === "shadow" ? { ...r, ...r.shadow } : r;
      lines.push(
        `### ${i + 1}. ${t.title}`,
        "",
        `负责：${t.owner}`,
        t.description,
        `自动推进：${t.automatic}`,
        `异常预案：${t.failure}`,
        "",
      );
    });
    lines.push(
      "## 当前情景",
      "",
      c.cause,
      `暂停：${c.stops}`,
      `继续：${c.continues}`,
      `恢复：${c.recovery}`,
      "",
    );
    if (!s.uncertain)
      lines.push(
        `示例储备 ${money(s.reserve)}；策略现金 ${money(s.cash)}；占用 ${money(s.hold)}；持仓 ${money(s.marketValue)}；策略 NAV ${money(s.nav)}；损益 ${signed(s.pnl)}。这些不是当前真实余额。`,
        "",
      );
    else lines.push("此情景资源待核实，不提供虚构的精确余额。", "");
    lines.push(`## 全部 ${C.scenarios.length} 种运行情景`, "");
    C.scenarios.forEach((scenario) => {
      lines.push(
        `### ${scenario.name}`,
        "",
        scenario.cause,
        `适用：${scenario.modes.map((id) => modeMap[id].name).join(" / ")}`,
        `只暂停：${scenario.stops}`,
        `必须继续：${scenario.continues}`,
        `恢复与处置：${scenario.recovery}`,
        `负责人：${scenario.owner}`,
        "",
      );
    });
    lines.push("## 26 项闸门与恢复预案", "");
    C.gates.forEach((g) =>
      lines.push(
        `### ${g.code} ${g.name}`,
        "",
        `处置：${KINDS[g.kind]}；范围：${g.scope}`,
        `防什么：${g.threat}`,
        `规则：${g.rule}`,
        `必须继续：${g.continues}`,
        `恢复：${g.recovery}`,
        `现状/证据：${g.current}（${g.evidence}，不是实时监控）`,
        "",
      ),
    );
    lines.push("## 本次架构复核与取舍", "");
    C.revisions.forEach((r) =>
      lines.push(
        `### ${r.name}`,
        "",
        `原歧义：${r.before}`,
        `修订：${r.after}`,
        `取舍：${r.tradeoff}`,
        `待讨论：${r.question}`,
        "",
      ),
    );
    lines.push("## 讨论意见（不构成交易授权）", "");
    const noteRows = [
      ...Object.entries(drafts).map(([id, d]) => ({
        ...d,
        item_id: id,
        source: "仅本地草稿",
      })),
      ...shared.decisions.map((d) => ({ ...d, source: "共享意见" })),
    ];
    if (!noteRows.length) lines.push("暂无已保存意见。", "");
    noteRows.forEach((d) =>
      lines.push(
        `### ${itemMap[d.item_id]?.name || itemMap[d.item_id]?.title || d.item_id} / ${d.source}`,
        "",
        `状态：${STATUS[d.status] || d.status}；记录人：${d.updated_by}；跟进：${d.owner || "未定"}`,
        d.rationale,
        "",
      ),
    );
    shared.comments.forEach((c) =>
      lines.push(`- 共享批注 ${c.item_id} / ${c.author}：${c.body}`),
    );
    lines.push(
      "",
      `规范来源：docs/${C.meta.spec}`,
      "未连接真实 QMT，不代表现网已经具备完整新架构。",
    );
    const blob = new Blob([lines.join("\n")], {
        type: "text/markdown;charset=utf-8",
      }),
      url = URL.createObjectURL(blob),
      a = document.createElement("a");
    a.href = url;
    a.download = "hydra-blueprint-discussion-" + C.meta.date + ".md";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("已导出完整讲解、26 项预案和已保存意见；不含 API key。");
  }

  $("play").addEventListener("click", play);
  $("previous").addEventListener("click", () => {
    pause();
    setStage(state.step - 1);
  });
  $("next").addEventListener("click", () => {
    pause();
    setStage(state.step + 1);
  });
  $("reset").addEventListener("click", () => {
    pause();
    state.resolved = false;
    setStage(0);
  });
  $("speed").addEventListener("change", () => {
    if (timer) {
      pause();
      play();
    }
  });
  $("scenario-select").addEventListener("change", (e) =>
    chooseScenario(e.target.value),
  );
  $("gate-search").addEventListener("input", renderGates);
  $("gate-kind").addEventListener("change", renderGates);
  $("gate-mode").addEventListener("change", renderGates);
  $("discuss-step").addEventListener("click", () =>
    openItem(C.stages[state.step].id),
  );
  $("show-math").addEventListener("click", showMath);
  $("close-dialog").addEventListener("click", closeDialog);
  $("detail-dialog").addEventListener("click", (e) => {
    if (e.target === $("detail-dialog")) {
      const r = e.target.getBoundingClientRect();
      if (
        e.clientX < r.left ||
        e.clientX > r.right ||
        e.clientY < r.top ||
        e.clientY > r.bottom
      )
        closeDialog();
    }
  });
  $("detail-dialog").addEventListener("cancel", () => {
    activeItem = null;
  });
  $("glossary-button").addEventListener("click", () => {
    activeItem = null;
    openDialog(
      "用业务语言，理解这几个词",
      "A SMALL GLOSSARY",
      `<dl class="glossary-list">${C.glossary.map((g) => `<div><dt>${esc(g.term)}</dt><dd>${esc(g.meaning)}</dd></div>`).join("")}</dl>`,
    );
  });
  $("connect-notes").addEventListener("click", connectDialog);
  $("export-notes").addEventListener("click", exportNotes);
  $("export-notes-bottom").addEventListener("click", exportNotes);
  $("copy-link").addEventListener("click", async () => {
    writeHash();
    try {
      await navigator.clipboard.writeText(location.href);
      toast(
        ["127.0.0.1", "localhost"].includes(location.hostname)
          ? "已复制本机预览链接；同伴不能从另一台电脑访问。上线后再分享正式地址。"
          : "已复制当前情景与步骤的链接；链接不含意见或凭据。",
      );
    } catch (_) {
      activeItem = null;
      openDialog(
        "复制这一幕的链接",
        "只包含情景、模式与步骤",
        `<p class="detail-lead">浏览器不允许自动复制，请手动复制下方链接。若这是 localhost 预览，同伴不能直接从另一台电脑访问；部署到 Dashboard 后再分享正式地址。</p><label class="connect-form">分享链接<input id="share-url" readonly value="${esc(location.href)}" style="width:100%;padding:10px"></label>`,
      );
      $("share-url").select();
    }
  });
  document.addEventListener("click", (e) => {
    const t = e.target.closest(
      "[data-view],[data-mode],[data-step],[data-scenario],[data-item],[data-category],[data-explore-mode]",
    );
    if (t && !t.disabled) {
      if (t.dataset.view) {
        e.preventDefault();
        showView(t.dataset.view);
      } else if (t.dataset.mode) chooseMode(t.dataset.mode);
      else if (t.dataset.step !== undefined) {
        pause();
        setStage(Number(t.dataset.step));
      } else if (t.dataset.scenario) chooseScenario(t.dataset.scenario);
      else if (t.dataset.item) openItem(t.dataset.item);
      else if (t.dataset.category) renderScenarios(t.dataset.category);
      else if (t.dataset.exploreMode) {
        chooseMode(t.dataset.exploreMode);
        state.scenario = "normal";
        state.resolved = false;
        setStage(0);
        showView("journey");
      }
    }
    const id = e.target.closest("button")?.id;
    if (id === "recover-scenario") {
      pause();
      state.resolved = true;
      renderJourney();
      writeHash();
      toast("已注入演示恢复证据；并非真实环境恢复。");
    }
    if (id === "continue-recovery") {
      setStage(state.step + 1);
    }
    if (id === "back-normal") {
      chooseScenario("normal");
    }
    if (id === "disconnect-notes") {
      keyInMemory = "";
      sharedConnected = false;
      shared = { comments: [], decisions: [], updated_at: null };
      renderMinutes();
      closeDialog();
      toast("已断开并清除内存中的凭据；本地草稿保留。");
    }
  });
  document.addEventListener("keydown", (e) => {
    const t = e.target.closest('g[role="button"][data-step]');
    if (t && ["Enter", " "].includes(e.key)) {
      e.preventDefault();
      pause();
      setStage(Number(t.dataset.step));
    }
  });
  document.addEventListener("submit", async (e) => {
    if (e.target.id === "note-form") {
      e.preventDefault();
      await saveNote(e.target);
    }
    if (e.target.id === "connect-form") {
      e.preventDefault();
      const input = $("notes-key");
      keyInMemory = input.value.trim();
      input.value = "";
      const button = $("connect-submit");
      button.disabled = true;
      try {
        await refreshNotes();
        closeDialog();
        toast("共享讨论已连接。没有上传本地草稿，也没有访问交易接口。");
      } catch (error) {
        keyInMemory = "";
        sharedConnected = false;
        renderMinutes();
        toast(
          "未连接：" +
            (error.name === "AbortError" ? "请求超时" : error.message),
        );
      } finally {
        if (button.isConnected) button.disabled = false;
      }
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      pause();
      renderMap();
    }
  });
  motion.addEventListener("change", () => {
    pause();
    renderMap();
  });
  window.addEventListener("hashchange", () => {
    pause();
    state = readHash();
    renderJourney();
    showView(state.view, false);
  });
  window.addEventListener("pagehide", () => {
    pause();
    clearTimeout(toastTimer);
    keyInMemory = "";
    sharedConnected = false;
    renderMinutes();
  });
  state = readHash();
  renderWorlds();
  renderScenarios();
  renderGates();
  renderRevisions();
  renderMinutes();
  renderJourney();
  showView(state.view, false);
})();
