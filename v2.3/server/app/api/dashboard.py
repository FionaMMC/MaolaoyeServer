"""GET /dashboard — 量化分析师向多视图 dashboard。

设计：
- 单 HTML，Chart.js CDN，0 服务端模板
- 5 个 tab：概览 / 收益 / 风险 / 策略内部 / 交易
- 时间窗切换：7d / 30d / 90d / ytd / 1y / all
- API key 仅存 localStorage，不上传
- 自动 60s 刷新当前 tab
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QMT Multi-Strategy Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, "PingFang HK", "Microsoft YaHei", sans-serif;
      background: #0f1419; color: #d4d8de;
      padding: 16px; min-height: 100vh;
    }
    .header {
      display: flex; justify-content: space-between; align-items: center;
      padding: 12px 20px; background: #1a2027; border-radius: 8px;
      margin-bottom: 12px; border-left: 4px solid #4ea1ff;
    }
    .header h1 { font-size: 1.25em; color: #fff; font-weight: 600; }
    .header .meta { font-size: 0.78em; color: #8a93a0; margin-top: 2px; }
    .toolbar {
      display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    }
    .toolbar select, .toolbar button {
      background: #0f1419; color: #d4d8de;
      border: 1px solid #2a3340; padding: 5px 10px;
      border-radius: 4px; font-size: 0.85em; cursor: pointer;
    }
    .toolbar button.primary { background: #4ea1ff; color: #fff; border-color: #4ea1ff; }
    .toolbar button.primary:hover { background: #6db4ff; }
    .toolbar button:hover { background: #2a3340; }

    .tabs {
      display: flex; gap: 4px; margin-bottom: 12px; padding: 4px;
      background: #1a2027; border-radius: 8px;
    }
    .tab {
      padding: 8px 14px; cursor: pointer; border-radius: 6px;
      font-size: 0.92em; color: #8a93a0;
      transition: all 0.15s;
    }
    .tab:hover { background: #2a3340; color: #d4d8de; }
    .tab.active { background: #4ea1ff; color: #fff; font-weight: 500; }
    .tab .icon { margin-right: 6px; }

    .view { display: none; }
    .view.active { display: block; }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 12px;
    }
    .grid-4 {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
    }
    .card {
      background: #1a2027; border-radius: 8px; padding: 14px;
      border: 1px solid #2a3340;
    }
    .card h2 {
      font-size: 0.95em; color: #4ea1ff; margin-bottom: 10px;
      padding-bottom: 6px; border-bottom: 1px solid #2a3340;
      font-weight: 600; display: flex; justify-content: space-between;
    }
    .card h2 .hint { font-size: 0.75em; color: #8a93a0; font-weight: normal; }
    .card.wide { grid-column: 1 / -1; }
    .card.tall { min-height: 320px; }

    .kpi {
      background: #0f1419; padding: 12px; border-radius: 6px;
      border-left: 3px solid #4ea1ff;
    }
    .kpi-label { font-size: 0.72em; color: #8a93a0; text-transform: uppercase;
                 letter-spacing: 0.5px; }
    .kpi-value { font-size: 1.5em; font-weight: 600; color: #fff; margin-top: 4px;
                 font-family: 'SF Mono', Monaco, monospace; }
    .kpi-value.pos { color: #4ade80; }
    .kpi-value.neg { color: #f87171; }
    .kpi-value.warn { color: #fbbf24; }
    .kpi-value.muted { color: #8a93a0; }
    .kpi-value.small { font-size: 1.05em; }
    .kpi-sub { font-size: 0.72em; color: #8a93a0; margin-top: 4px; }

    table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
    table th, table td {
      padding: 6px 8px; text-align: left;
      border-bottom: 1px solid #2a3340;
    }
    table th { color: #8a93a0; font-weight: 500; font-size: 0.78em;
               text-transform: uppercase; letter-spacing: 0.3px; }
    table tr:hover { background: #0f1419; }
    .num { font-family: 'SF Mono', Monaco, monospace; text-align: right; }
    .num.pos { color: #4ade80; }
    .num.neg { color: #f87171; }
    .badge {
      display: inline-block; padding: 1px 7px; border-radius: 3px;
      font-size: 0.75em; font-weight: 500;
    }
    .badge-success { background: #064e3b; color: #4ade80; }
    .badge-warn { background: #422006; color: #fbbf24; }
    .badge-danger { background: #4c0519; color: #f87171; }
    .badge-info { background: #1e3a5f; color: #93c5fd; }

    .loading { color: #8a93a0; font-style: italic; padding: 16px; text-align: center; }
    .error { color: #f87171; background: #2a0a0a; padding: 10px; border-radius: 6px;
             font-size: 0.85em; }
    .chart-container { position: relative; height: 240px; }
    .chart-container.tall { height: 320px; }

    #login-modal {
      position: fixed; top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0,0,0,0.85); display: none;
      align-items: center; justify-content: center; z-index: 1000;
    }
    .modal-box {
      background: #1a2027; padding: 24px; border-radius: 8px;
      width: 90%; max-width: 400px; border: 1px solid #2a3340;
    }
    .modal-box h3 { color: #fff; margin-bottom: 12px; }
    .modal-box input {
      width: 100%; padding: 10px; margin: 8px 0;
      background: #0f1419; border: 1px solid #2a3340; color: #fff;
      border-radius: 4px; font-family: monospace;
    }
    .modal-box button {
      width: 100%; padding: 10px; background: #4ea1ff; color: #fff;
      border: none; border-radius: 4px; cursor: pointer; font-weight: 500;
    }

    #health-strip {
      display: flex; flex-wrap: wrap; gap: 16px; align-items: center;
      padding: 8px 16px; background: #131a21; border-radius: 6px;
      margin-bottom: 12px; font-size: 0.83em; border: 1px solid #2a3340;
    }
    #health-strip span { color: #8a93a0; }
    #health-strip b { color: #d4d8de; }
    .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
           vertical-align: middle; margin-right: 3px; }
    .dot.ok  { background: #4ade80; }
    .dot.bad { background: #f87171; }
    .stale   { color: #fbbf24; }
    .crit    { color: #f87171; }

    /* 2026 live-operations renovation: dense, restrained, terminal-first. */
    :root {
      --bg: #070a0f; --surface: #0e141c; --surface-2: #121a24;
      --line: #202b39; --text: #eef4fb; --muted: #687789;
      --accent: #51c8f2; --positive: #40d6a0; --negative: #ff647c;
      --warning: #f6bd58; --mono: "SFMono-Regular", Consolas, monospace;
    }
    body {
      max-width: 1720px; margin: 0 auto; padding: 0 24px 44px;
      background: radial-gradient(circle at 72% -30%, rgba(54,111,142,.13), transparent 38%), var(--bg);
      color: var(--text); font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
      font-size: 14px;
    }
    .header {
      position: sticky; top: 0; z-index: 30; min-height: 76px; margin: 0 -24px;
      padding: 13px 24px; border: 0; border-bottom: 1px solid var(--line); border-radius: 0;
      background: rgba(7,10,15,.92); backdrop-filter: blur(18px);
    }
    .header h1 { font-size: 1.12em; letter-spacing: .03em; font-weight: 680; }
    .header .meta { color: var(--muted); font: 10px/1.4 var(--mono); margin-top: 6px; }
    .brand-kicker { color: var(--accent); font: 9px/1 var(--mono); letter-spacing: .18em; margin-bottom: 7px; }
    .toolbar select, .toolbar button {
      height: 34px; border-radius: 7px; border-color: #263444; background: #0d131b;
      color: #cdd9e6; font-size: .78em; padding: 0 10px;
    }
    .toolbar button.primary { background: var(--accent); border-color: var(--accent); color: #061017; font-weight: 750; }
    .toolbar button.primary:hover { background: #75d7f8; }
    #health-strip {
      margin: 0 -24px 14px; min-height: 36px; padding: 0 24px; border-width: 0 0 1px;
      border-radius: 0; background: #090d13; color: var(--muted); gap: 22px; font: 10px var(--mono);
      overflow-x: auto; white-space: nowrap;
    }
    #health-strip b { color: #d8e3ee; }
    .tabs {
      position: sticky; top: 76px; z-index: 25; gap: 2px; margin: 0 -24px 18px; padding: 7px 24px;
      border-radius: 0; border-bottom: 1px solid var(--line); background: rgba(9,13,19,.94);
      backdrop-filter: blur(14px); overflow-x: auto;
    }
    .tabs, #health-strip { scrollbar-width: none; }
    .tabs::-webkit-scrollbar, #health-strip::-webkit-scrollbar { display: none; }
    .tab { white-space: nowrap; padding: 8px 12px; border: 1px solid transparent; border-radius: 7px; font-size: .78em; }
    .tab:hover { background: #111923; }
    .tab.active { background: rgba(81,200,242,.09); border-color: rgba(81,200,242,.22); color: #e9f9ff; font-weight: 620; }
    .tab .icon { color: var(--accent); font: 9px var(--mono); margin-right: 7px; }
    .grid, .grid-4 { gap: 10px; }
    .grid-4 { grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); }
    .card {
      min-width: 0;
      border: 1px solid var(--line); border-radius: 11px; background: var(--surface);
      padding: 15px; box-shadow: 0 10px 35px rgba(0,0,0,.12);
    }
    .card h2 {
      min-height: 30px; margin: -3px 0 12px; padding: 0 0 10px; color: #cdd9e5;
      border-color: rgba(135,157,184,.13); font-size: .74em; letter-spacing: .07em;
      font-family: var(--mono); text-transform: uppercase;
    }
    .card h2 .hint { color: var(--muted); text-transform: none; letter-spacing: 0; }
    .kpi {
      min-height: 107px; padding: 14px; border: 1px solid var(--line); border-left: 1px solid var(--line);
      border-radius: 10px; background: linear-gradient(145deg, #101720, #0d131b); position: relative; overflow: hidden;
    }
    .kpi::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 2px; background: #253341; }
    .kpi:has(.kpi-value.pos)::after { background: var(--positive); }
    .kpi:has(.kpi-value.neg)::after { background: var(--negative); }
    .kpi:has(.kpi-value.warn)::after { background: var(--warning); }
    .kpi-label { color: #718196; font: 9px/1.3 var(--mono); letter-spacing: .09em; }
    .kpi-value { margin-top: 13px; font: 22px/1 var(--mono); letter-spacing: -.04em; }
    .kpi-value.pos, .num.pos { color: var(--positive); }
    .kpi-value.neg, .num.neg { color: var(--negative); }
    .kpi-value.warn { color: var(--warning); }
    .kpi-sub { margin-top: 10px; color: #5e6e81; font: 9px/1.25 var(--mono); }
    table { font-size: .72em; }
    table th { height: 34px; color: #5c6b7e; background: #0b1017; font: 9px var(--mono); }
    table td { height: 38px; color: #aebbc9; }
    table th, table td { padding: 0 11px; border-color: rgba(135,157,184,.11); }
    table tr:hover { background: rgba(81,200,242,.025); }
    .badge { border-radius: 4px; font-family: var(--mono); }
    .chart-container { height: 270px; }
    .chart-container.tall { height: 330px; }
    .live-heading { display: flex; justify-content: space-between; align-items: end; gap: 16px; margin: 3px 0 15px; }
    .live-heading h2 { margin: 0; font-size: 1.08em; letter-spacing: -.01em; }
    .live-heading p { margin: 5px 0 0; color: var(--muted); font-size: .72em; }
    .live-asof { color: var(--muted); font: 9px var(--mono); }
    .live-main-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(300px, .78fr); gap: 10px; margin-top: 10px; }
    .live-three-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }
    .control-list { display: grid; gap: 1px; margin: -15px; background: rgba(135,157,184,.1); }
    .control-row { padding: 11px 14px; background: var(--surface); display: grid; grid-template-columns: 9px minmax(0,1fr) auto; gap: 10px; align-items: center; }
    .control-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
    .control-dot.ok { background: var(--positive); }
    .control-dot.warn { background: var(--warning); }
    .control-dot.bad { background: var(--negative); }
    .control-name { color: #c4d0dc; font-size: .74em; }
    .control-desc { color: var(--muted); font: 9px/1.4 var(--mono); margin-top: 3px; }
    .control-value { color: #cdd9e6; font: 9px var(--mono); text-align: right; }
    .metric-list { display: grid; gap: 12px; }
    .metric-line { display: grid; grid-template-columns: 105px minmax(0,1fr) 58px; gap: 10px; align-items: center; }
    .metric-name { color: #aebbc9; font: 9px var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .metric-track { height: 5px; border-radius: 9px; background: #1d2834; overflow: hidden; }
    .metric-track i { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg,#4d86ef,var(--accent)); }
    .metric-value { text-align: right; color: #c5d0dc; font: 9px var(--mono); }
    .coverage-list { display: grid; gap: 7px; }
    .coverage-item { padding: 9px 10px; border: 1px solid rgba(135,157,184,.13); border-radius: 7px; background: #0b1118; }
    .coverage-top { display: flex; justify-content: space-between; gap: 8px; color: #c8d3df; font-size: .7em; }
    .coverage-next { margin-top: 5px; color: var(--muted); font: 8px/1.45 var(--mono); }
    .empty-state { min-height: 76px; display: grid; place-items: center; color: var(--muted); font: 9px/1.5 var(--mono); text-align: center; }
    .alert-stack { display: grid; gap: 7px; }
    .alert-item { padding: 9px 10px; border: 1px solid rgba(135,157,184,.13); border-left: 3px solid #73a6ff; border-radius: 7px; background: #0b1118; }
    .alert-item.critical { border-left-color: var(--negative); }
    .alert-item.warn { border-left-color: var(--warning); }
    .alert-title { color: #c7d2de; font-size: .72em; line-height: 1.45; }
    .alert-meta { margin-top: 4px; color: var(--muted); font: 8px var(--mono); }
    .live-note { margin-top: 13px; padding-top: 10px; border-top: 1px solid rgba(135,157,184,.13); color: var(--muted); font: 8px/1.45 var(--mono); }
    #live-orders { overflow-x: auto; }
    .modal-box { border-radius: 13px; background: #0d131b; border-color: #293847; box-shadow: 0 30px 100px rgba(0,0,0,.55); }
    .modal-box input { border-radius: 7px; background: #080d13; border-color: #2b3a49; }
    .modal-box button { border-radius: 7px; background: var(--accent); color: #061017; font-weight: 750; }
    @media (max-width: 1100px) {
      .live-three-grid { grid-template-columns: 1fr; }
      .live-three-grid > .card { grid-column: auto !important; }
    }
    @media (max-width: 820px) {
      body { padding: 0 13px 30px; }
      .header, #health-strip, .tabs { margin-left: -13px; margin-right: -13px; padding-left: 13px; padding-right: 13px; }
      .header { position: relative; align-items: flex-start; }
      .tabs { top: 0; }
      .live-main-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div id="login-modal">
    <div class="modal-box">
      <div class="brand-kicker">AURORA QUANT / READ ONLY</div>
      <h3>Live Operations Control</h3>
      <p style="color:#8a93a0; font-size:0.9em; margin-bottom:12px;">
        API Key 仅保存在当前浏览器 localStorage，不写入页面或 URL。
      </p>
      <input type="password" id="api-key-input"
             placeholder="API Key"
             onkeypress="if(event.key==='Enter') saveKey()">
      <button onclick="saveKey()">登录</button>
    </div>
  </div>

  <div class="header">
    <div>
      <div class="brand-kicker">AURORA QUANT / LIVE CONTROL</div>
      <h1>实盘运营与风险监控</h1>
      <div class="meta" id="meta">Loading...</div>
    </div>
    <div class="toolbar">
      <label style="font-size:0.85em;color:#8a93a0;">实例:</label>
      <select id="instSel" onchange="onInstanceChange()" disabled>
        <option value="">加载实例...</option>
      </select>
      <label style="font-size:0.85em;color:#8a93a0;margin-left:8px;">期间:</label>
      <select id="period-select" onchange="onPeriodChange()">
        <option value="7d">最近 7 天</option>
        <option value="30d" selected>最近 30 天</option>
        <option value="90d">最近 90 天</option>
        <option value="180d">最近 180 天</option>
        <option value="ytd">年初至今</option>
        <option value="1y">最近 1 年</option>
        <option value="all">全部</option>
      </select>
      <button class="primary" onclick="refreshAll()">↻ 刷新</button>
    </div>
  </div>

  <div id="health-strip"><span style="color:#8a93a0">加载中…</span></div>

  <div class="tabs">
    <div class="tab active" data-view="live" onclick="showTab('live')">
      <span class="icon">L1</span>实盘总控
    </div>
    <div class="tab" data-view="overview" onclick="showTab('overview')">
      <span class="icon">PF</span>组合概览
    </div>
    <div class="tab" data-view="returns" onclick="showTab('returns')">
      <span class="icon">PN</span>绩效归因
    </div>
    <div class="tab" data-view="risk" onclick="showTab('risk')">
      <span class="icon">RK</span>风险分析
    </div>
    <div class="tab" data-view="strategy" onclick="showTab('strategy')">
      <span class="icon">ST</span>策略状态
    </div>
    <div class="tab" data-view="trades" onclick="showTab('trades')">
      <span class="icon">EX</span>执行分析
    </div>
    <div class="tab" data-view="ops" onclick="showTab('ops')">
      <span class="icon">OP</span>运营审计
    </div>
  </div>

  <!-- 24h 实盘总控：只展示真实可观测数据，缺失遥测显式暴露。 -->
  <div class="view active" id="view-live">
    <div class="live-heading">
      <div><h2>Live Command Center</h2><p>风险、执行、对账和数据链路的一屏式值守视图</p></div>
      <div class="live-asof" id="live-asof">—</div>
    </div>
    <div class="grid-4" id="live-kpis"><div class="loading">读取实盘快照…</div></div>
    <div class="live-main-grid">
      <div class="card tall">
        <h2>NAV / CAPITAL TRAJECTORY <span class="hint" id="live-nav-hint">—</span></h2>
        <div class="chart-container"><canvas id="live-nav-chart"></canvas></div>
      </div>
      <div class="card tall">
        <h2>CONTROL LEDGER <span class="hint">read only</span></h2>
        <div class="control-list" id="live-controls"><div class="loading">检查控制项…</div></div>
      </div>
    </div>
    <div class="live-three-grid">
      <div class="card">
        <h2>EXECUTION FUNNEL · 30D <span class="hint" id="execution-scope">—</span></h2>
        <div id="execution-funnel"><div class="loading">读取订单…</div></div>
      </div>
      <div class="card">
        <h2>POSITION INVENTORY <span class="hint">数量口径</span></h2>
        <div id="position-inventory"><div class="loading">读取持仓…</div></div>
      </div>
      <div class="card">
        <h2>TELEMETRY COVERAGE <span class="hint">P0 / P1 gaps</span></h2>
        <div id="telemetry-coverage"><div class="loading">检查覆盖率…</div></div>
      </div>
      <div class="card" style="grid-column:span 2">
        <h2>RECENT ORDER LIFECYCLE <span class="hint">最新 12 笔</span></h2>
        <div id="live-orders"><div class="loading">读取订单生命周期…</div></div>
      </div>
      <div class="card">
        <h2>ACTIONABLE ALERTS <span class="hint">critical first</span></h2>
        <div id="live-alerts"><div class="loading">运行告警检查…</div></div>
      </div>
    </div>
  </div>

  <!-- 概览 -->
  <div class="view" id="view-overview">
    <div class="grid-4" id="kpis-overview"><div class="loading">Loading...</div></div>
    <div style="height:12px"></div>
    <div class="grid">
      <div class="card tall">
        <h2>NAV 走势 <span class="hint" id="nav-hint">—</span></h2>
        <div class="chart-container"><canvas id="nav-chart"></canvas></div>
      </div>
      <div class="card tall">
        <h2>每日收益 (%)</h2>
        <div class="chart-container"><canvas id="ret-chart"></canvas></div>
      </div>
      <div class="card wide">
        <h2>实例状态</h2>
        <div id="instance-state"><div class="loading">Loading...</div></div>
      </div>
      <div class="card wide">
        <h2>全部策略账本 <span class="hint">shadow 为仅虚拟记账、无订单</span></h2>
        <div id="portfolio-overview"><div class="loading">Loading...</div></div>
      </div>
    </div>
  </div>

  <!-- 收益分析 -->
  <div class="view" id="view-returns">
    <div class="grid-4" id="kpis-returns"><div class="loading">Loading...</div></div>
    <div style="height:12px"></div>
    <div class="grid">
      <div class="card tall wide">
        <h2>NAV trajectory <span class="hint" id="nav2-hint">—</span></h2>
        <div class="chart-container tall"><canvas id="nav-chart2"></canvas></div>
      </div>
      <div class="card">
        <h2>周度收益</h2>
        <div id="weekly-returns"><div class="loading">Loading...</div></div>
      </div>
      <div class="card">
        <h2>月度收益</h2>
        <div id="monthly-returns"><div class="loading">Loading...</div></div>
      </div>
      <div class="card wide">
        <h2>年度收益</h2>
        <div id="yearly-returns"><div class="loading">Loading...</div></div>
      </div>
    </div>
  </div>

  <!-- 风险分析 -->
  <div class="view" id="view-risk">
    <div class="grid-4" id="kpis-risk"><div class="loading">Loading...</div></div>
    <div style="height:12px"></div>
    <div class="grid">
      <div class="card tall wide">
        <h2>Drawdown 曲线（水下） <span class="hint">相对历史最高点</span></h2>
        <div class="chart-container tall"><canvas id="dd-chart"></canvas></div>
      </div>
      <div class="card tall">
        <h2>每日收益分布</h2>
        <div class="chart-container"><canvas id="ret-hist-chart"></canvas></div>
      </div>
      <div class="card">
        <h2>风险摘要</h2>
        <div id="risk-summary"><div class="loading">Loading...</div></div>
      </div>
    </div>
  </div>

  <!-- 策略内部 -->
  <div class="view" id="view-strategy">
    <div class="grid">
      <div class="card wide">
        <h2>V20H 策略状态</h2>
        <div id="strategy-state"><div class="loading">Loading...</div></div>
      </div>
      <div class="card">
        <h2>黑名单 (合计 <span id="bl-total">-</span>)</h2>
        <div id="blacklist"><div class="loading">Loading...</div></div>
      </div>
      <div class="card">
        <h2>最近 REJECTED Top 10</h2>
        <div id="recent-rejected"><div class="loading">Loading...</div></div>
      </div>
      <div class="card wide">
        <h2>对账分叉告警 (bookkeeping_divergence)</h2>
        <div id="bk-divergence"><div class="loading">Loading...</div></div>
      </div>
    </div>
  </div>

  <!-- 交易分析 -->
  <div class="view" id="view-trades">
    <div class="grid-4" id="kpis-trades"><div class="loading">Loading...</div></div>
    <div style="height:12px"></div>
    <div class="grid">
      <div class="card wide">
        <h2>Orders 状态矩阵 <span class="hint">按当前期间</span></h2>
        <div id="orders-matrix"><div class="loading">Loading...</div></div>
      </div>
    </div>
  </div>

  <!-- 运营与对账 -->
  <div class="view" id="view-ops">
    <div class="grid">
      <div class="card wide">
        <h2>告警列表 <span class="hint">critical → warn → info</span></h2>
        <div id="ops-alerts"><div class="loading">Loading...</div></div>
      </div>
      <div class="card wide">
        <h2>管线运行记录 <span class="hint">最近 14 天</span></h2>
        <div id="ops-runs"><div class="loading">Loading...</div></div>
      </div>
      <div class="card">
        <h2>行情新鲜度</h2>
        <div id="ops-freshness"><div class="loading">Loading...</div></div>
      </div>
      <div class="card">
        <h2>快照完整性</h2>
        <div id="ops-integrity"><div class="loading">Loading...</div></div>
      </div>
      <div class="card wide">
        <h2>隔夜仓位异常 <span class="hint">倍增/归零检测</span></h2>
        <div id="ops-anomalies"><div class="loading">Loading...</div></div>
      </div>
    </div>
  </div>

  <script>
    const API_BASE = window.location.origin;
    let API_KEY = localStorage.getItem('qmt_api_key') || '';
    let currentTab = 'live';
    let charts = {};

    async function saveKey() {
      const k = document.getElementById('api-key-input').value.trim();
      if (!k) return;
      localStorage.setItem('qmt_api_key', k);
      API_KEY = k;
      document.getElementById('login-modal').style.display = 'none';
      await initializeDashboard();
    }

    function checkAuth() {
      if (!API_KEY) {
        document.getElementById('login-modal').style.display = 'flex';
        return false;
      }
      return true;
    }

    async function api(path) {
      const r = await fetch(API_BASE + path, {
        headers: { 'Authorization': 'Bearer ' + API_KEY }
      });
      if (r.status === 401) {
        localStorage.removeItem('qmt_api_key');
        API_KEY = '';
        checkAuth();
        throw new Error('401 unauthorized');
      }
      const body = await r.json();
      if (body.code !== 0) throw new Error(body.message || 'API error');
      return body.data;
    }

    // ── 共享工具函数 ────────────────────────────────────────────
    function fmtNum(x){ return x==null?'—':Number(x).toLocaleString(); }
    function fmtPct(x){ return x==null?'—':(x*100).toFixed(2)+'%'; }
    function staleClass(lagDays){ if(lagDays==null) return ''; return lagDays>5?'crit':(lagDays>1?'stale':''); }
    function esc(x){ return String(x ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
    function fmtLiveBps(x){ return x==null?'—':Number(x).toFixed(1)+' bp'; }
    function ageText(seconds){
      if(seconds==null) return '—';
      if(seconds<60) return Math.round(seconds)+'s';
      if(seconds<3600) return Math.floor(seconds/60)+'m';
      if(seconds<86400) return Math.floor(seconds/3600)+'h';
      return Math.floor(seconds/86400)+'d';
    }

    let META = null, LAST_VERSION = null;

    function query(params) {
      return new URLSearchParams(params).toString();
    }

    let INSTANCE_META = {};

    async function loadInstanceOptions() {
      const health = await api('/admin/health');
      const select = document.getElementById('instSel');
      const previous = localStorage.getItem('qmt_dashboard_instance') || select.value;
      const instances = health.instances || [];
      if (!instances.length) throw new Error('服务器尚无可展示的实例');
      INSTANCE_META = Object.fromEntries(instances.map(i => [i.instance_id, i]));
      select.innerHTML = instances.map(i =>
        `<option value="${i.instance_id}">${i.display_name || i.instance_id}${i.is_shadow ? ' [shadow]' : ''}</option>`
      ).join('');
      select.value = instances.some(i => i.instance_id === previous)
        ? previous : instances[0].instance_id;
      select.disabled = false;
    }

    async function metaPoll(){
      try{
        const r = await api('/admin/dashboard-meta?' + query({instance_id: getInstanceId()}));
        META = r;
        renderHealthStrip(META);
        const v = JSON.stringify(META.version);
        if(v !== LAST_VERSION){ LAST_VERSION = v; onVersionChanged(); }
      }catch(e){ renderHealthStripError(); }
    }

    function onVersionChanged(){
      if(typeof refreshAll==='function') refreshAll();
    }

    function renderHealthStrip(m){
      const a = m.alerts||{}; const fr = m.freshness||{};
      const lag = fr.market_lag_days;
      const lastRun = m.last_pipeline_run;
      const el = document.getElementById('health-strip');
      if(!el) return;
      el.innerHTML =
        '<span>账户 NAV <b>' + fmtNum(m.account_nav) + '</b></span>' +
        '<span>行情 <span class="' + staleClass(lag) + '">' + (fr.market_latest||'—') + (lag!=null?' ('+lag+'d)':'') + '</span></span>' +
        '<span>管线 <span class="dot ' + (lastRun&&lastRun.status==='ok'?'ok':'bad') + '"></span>' + (lastRun?lastRun.valid_date:'—') + '</span>' +
        '<span class="' + (a.critical?'crit':'') + '">告警 ' + (a.critical||0) + '🔴 / ' + (a.warn||0) + '🟡</span>' +
        '<span style="color:#8a93a0">as-of ' + (m.timestamp||'') + '</span>';
    }

    function renderHealthStripError(){
      const el=document.getElementById('health-strip');
      if(el) el.innerHTML='<span class="crit">⚠ dashboard-meta 不可达</span>';
    }

    function fmt(n, opts = {}) {
      if (n === null || n === undefined || (typeof n === 'number' && isNaN(n))) return '—';
      const sign = opts.sign && n > 0 ? '+' : '';
      if (opts.pct) return sign + (n * 100).toFixed(2) + '%';
      if (opts.bps) return sign + (n * 10000).toFixed(0) + 'bps';
      if (opts.cur) return (n < 0 ? '-¥' : '¥') + Math.abs(n).toLocaleString('en-US', {maximumFractionDigits: 0});
      if (opts.curMM) return (n < 0 ? '-¥' : '¥') + (Math.abs(n)/1e6).toFixed(2) + 'M';
      if (opts.dec !== undefined) return sign + n.toFixed(opts.dec);
      return sign + n.toString();
    }

    function colorOf(n) {
      if (n === null || n === undefined) return '';
      return n > 0 ? 'pos' : (n < 0 ? 'neg' : '');
    }

    function badge(text, type) {
      return `<span class="badge badge-${type}">${text}</span>`;
    }

    function getPeriod() {
      return document.getElementById('period-select').value;
    }

    function getInstanceId() {
      return document.getElementById('instSel').value;
    }

    function selectedQuery(extra = {}) {
      return query({instance_id: getInstanceId(), ...extra});
    }

    function navQuery(period) {
      return '/admin/nav-history?' + selectedQuery({period, limit: 1000});
    }

    function isShadowSelected() {
      return Boolean(INSTANCE_META[getInstanceId()]?.is_shadow);
    }

    function onPeriodChange() {
      refreshAll();
    }

    function onInstanceChange() {
      localStorage.setItem('qmt_dashboard_instance', getInstanceId());
      metaPoll();
      refreshAll();
    }

    function showTab(name) {
      currentTab = name;
      document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === name));
      document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
      refreshAll();
    }

    // ── 24h 实盘总控 ────────────────────────────────────────────
    function liveControlRows(snapshot) {
      const c = snapshot.controls || {}, fr = snapshot.freshness || {};
      const rows = [
        ['账本一致性', c.bookkeeping_divergences === 0 ? 'ok' : 'bad',
         c.bookkeeping_divergences === 0 ? '无分叉' : `${c.bookkeeping_divergences} 项`,
         '真实成交 ↔ 策略账本'],
        ['僵尸挂单', c.stale_pending_orders === 0 ? 'ok' : 'bad',
         String(c.stale_pending_orders ?? '—'), 'PENDING > 2 days'],
        ['价格保护', c.price_protection_utilization == null ? 'warn' :
         (c.price_protection_utilization <= 1 ? 'ok' : 'bad'),
         fmtLiveBps(c.max_price_offset_bps_observed), `硬约束 ${c.price_protection_limit_bps ?? '—'} bp`],
        ['快照完整性', c.snapshot_integrity_issues === 0 ? 'ok' : 'warn',
         c.snapshot_integrity_issues === 0 ? '通过' : `${c.snapshot_integrity_issues} 项`,
         'NAV freeze / gap'],
        ['隔夜仓位', c.overnight_position_anomalies === 0 ? 'ok' : 'bad',
         c.overnight_position_anomalies === 0 ? '正常' : `${c.overnight_position_anomalies} 项`,
         '单标的变化 > 50%'],
        ['行情 EOD', fr.market_lag_days == null ? 'warn' :
         (fr.market_lag_days <= 1 ? 'ok' : (fr.market_lag_days <= 3 ? 'warn' : 'bad')),
         fr.market_latest || '未接入', fr.market_lag_days == null ? '无探针' : `lag ${fr.market_lag_days}d`],
      ];
      return rows.map(([name,status,value,desc]) => `<div class="control-row">
        <i class="control-dot ${status}"></i><div><div class="control-name">${esc(name)}</div>
        <div class="control-desc">${esc(desc)}</div></div><div class="control-value">${esc(value)}</div>
      </div>`).join('');
    }

    function renderExecutionFunnel(execution) {
      const statuses = execution.status_counts || {};
      const total = Math.max(execution.orders_total || 0, 1);
      const metrics = [
        ['Submitted', execution.orders_total || 0, 100],
        ['Filled / Partial', (statuses.FILLED||0)+(statuses.PARTIAL||0),
         ((statuses.FILLED||0)+(statuses.PARTIAL||0))/total*100],
        ['Pending', statuses.PENDING||0, (statuses.PENDING||0)/total*100],
        ['Rejected', statuses.REJECTED||0, (statuses.REJECTED||0)/total*100],
      ];
      document.getElementById('execution-scope').textContent = execution.scope || '—';
      document.getElementById('execution-funnel').innerHTML = `
        <div class="metric-list">${metrics.map(m => `<div class="metric-line">
          <span class="metric-name">${m[0]}</span><span class="metric-track"><i style="width:${Math.max(0,Math.min(100,m[2]))}%"></i></span>
          <span class="metric-value">${m[1]}</span></div>`).join('')}</div>
        <div class="live-note">FILLED ${fmt(execution.filled_notional,{curMM:true})} · FEES ${fmt(execution.estimated_fees,{cur:true})}<br>
          REJECT ${fmt(execution.reject_rate,{pct:true})} · MAX SHORTFALL ${fmtLiveBps(execution.max_abs_shortfall_bps)}</div>`;
    }

    function renderPositionInventory(items) {
      const el = document.getElementById('position-inventory');
      if (!items || !items.length) {
        el.innerHTML = '<div class="empty-state">暂无持仓，或实例尚未完成账本快照</div>';
        return;
      }
      const top = items.slice(0, 8);
      const maxQty = Math.max(...top.map(i => Math.abs(i.quantity)), 1);
      el.innerHTML = `<div class="metric-list">${top.map(i => `<div class="metric-line">
        <span class="metric-name">${esc(i.symbol)}</span><span class="metric-track"><i style="width:${Math.abs(i.quantity)/maxQty*100}%"></i></span>
        <span class="metric-value">${fmtNum(i.quantity)}</span></div>`).join('')}</div>
        <div class="live-note">当前仅有数量，没有 raw mark price；不能据此判断权重、总暴露或集中度。</div>`;
    }

    function renderTelemetryCoverage(items) {
      document.getElementById('telemetry-coverage').innerHTML = `<div class="coverage-list">
        ${(items||[]).map(item => `<div class="coverage-item"><div class="coverage-top">
          <span>${esc(item.label)}</span>${badge(item.priority, item.priority==='P0'?'danger':'warn')}</div>
          <div class="coverage-next">${esc(item.next)}</div></div>`).join('')}</div>`;
    }

    function renderLiveOrders(items) {
      const el = document.getElementById('live-orders');
      if (!items || !items.length) {
        el.innerHTML = '<div class="empty-state">该实例在窗口内没有可映射订单</div>';
        return;
      }
      const statusType = s => s==='FILLED'?'success':(s==='REJECTED'?'danger':(s==='PENDING'?'warn':'info'));
      el.innerHTML = `<table><tr><th>Time</th><th>Symbol</th><th>Side</th><th class="num">Qty</th>
        <th class="num">Limit</th><th>Status</th></tr>${items.map(o => `<tr>
        <td>${esc(o.created_at || o.valid_date)}</td><td>${esc(o.symbol)}</td>
        <td class="${o.direction==='BUY'?'pos':'neg'}">${esc(o.direction)}</td>
        <td class="num">${fmtNum(o.quantity)}</td><td class="num">${fmt(o.limit_price,{dec:3})}</td>
        <td>${badge(o.status,statusType(o.status))}</td></tr>`).join('')}</table>`;
    }

    function renderLiveAlerts(alerts) {
      const severity = {critical:0,warn:1,info:2};
      const sorted = (alerts||[]).slice().sort((a,b)=>(severity[a.severity]??9)-(severity[b.severity]??9));
      const el = document.getElementById('live-alerts');
      if (!sorted.length) {
        el.innerHTML = '<div class="empty-state"><span style="color:var(--positive)">ALL CLEAR</span><br>当前检查未发现可操作告警</div>';
        return;
      }
      el.innerHTML = `<div class="alert-stack">${sorted.map(a => `<div class="alert-item ${esc(a.severity)}">
        <div class="alert-title">${esc(a.message)}</div><div class="alert-meta">${esc(a.category)} · ${esc(a.as_of)}</div>
      </div>`).join('')}</div>`;
    }

    async function renderLive() {
      try {
        const [snapshot, alerts, navData] = await Promise.all([
          api('/admin/ops/live-snapshot?' + selectedQuery({days:30})),
          api('/admin/alerts'), api(navQuery(getPeriod())),
        ]);
        const inst = snapshot.instance || {}, risk = snapshot.risk || {}, execution = snapshot.execution || {};
        document.getElementById('live-asof').textContent = `AS OF ${snapshot.as_of || '—'}`;
        document.getElementById('live-kpis').innerHTML = `
          ${kpiCard('NAV · EOD', fmt(inst.nav,{cur:true}), '', `${inst.nav_date||'—'} · cash ${fmt(inst.cash_ratio,{pct:true})}`)}
          ${kpiCard('Day P&L · EOD', fmt(risk.daily_pnl,{cur:true}), colorOf(risk.daily_pnl), fmt(risk.daily_return,{pct:true,sign:true}))}
          ${kpiCard('Current Drawdown', fmt(risk.current_drawdown,{pct:true}), risk.current_drawdown<0?'neg':'pos', 'from high-water mark')}
          ${kpiCard('20D Ann. Vol', fmt(risk.rolling_volatility_20d,{pct:true}), risk.rolling_volatility_20d==null?'warn':'', `VaR ${fmt(risk.historical_var_95_1d,{pct:true})} · ES ${fmt(risk.expected_shortfall_95_1d,{pct:true})}`)}
          ${kpiCard('Fill Rate · 30D', fmt(execution.fill_rate,{pct:true}), execution.fill_rate==null?'warn':(execution.fill_rate>=.9?'pos':(execution.fill_rate<.7?'neg':'warn')), `${execution.orders_total||0} orders`)}
          ${kpiCard('Exec Shortfall', fmtLiveBps(execution.weighted_shortfall_bps), execution.weighted_shortfall_bps==null?'warn':(Math.abs(execution.weighted_shortfall_bps)<=10?'pos':'warn'), 'directional · notional weighted')}`;
        document.getElementById('live-nav-hint').textContent = `${inst.nav_date||'—'} · ${risk.sample_days||0} risk samples`;
        document.getElementById('live-controls').innerHTML = liveControlRows(snapshot);
        renderNavChart('live-nav-chart', navData, '#51c8f2');
        renderExecutionFunnel(execution);
        renderPositionInventory(snapshot.positions);
        renderTelemetryCoverage(snapshot.coverage_gaps);
        renderLiveOrders(snapshot.recent_orders);
        renderLiveAlerts(alerts.alerts);
      } catch (e) {
        document.getElementById('live-kpis').innerHTML = `<div class="error">${esc(e.message)}</div>`;
      }
    }

    // ── 概览 ────────────────────────────────────────────────────
    async function renderOverview() {
      const period = getPeriod();
      try {
        const [health, summary, navData, portfolio] = await Promise.all([
          api('/admin/health'),
          api('/admin/metrics/summary?' + selectedQuery({period})),
          api(navQuery(period)),
          api('/admin/portfolio-overview'),
        ]);

        // KPIs
        const cur = summary.cumulative_return;
        const ann = summary.annualized_return;
        const sh = summary.sharpe;
        const mdd = summary.max_drawdown;
        document.getElementById('kpis-overview').innerHTML = `
          ${kpiCard('累计收益', fmt(cur, {pct:true, sign:true}), colorOf(cur),
                   `${summary.n_days} 个交易日`)}
          ${kpiCard('年化收益', fmt(ann, {pct:true, sign:true}), colorOf(ann),
                   '基于 252 交易日')}
          ${kpiCard('Sharpe', fmt(sh, {dec:2}),
                   sh === null ? 'muted' : (sh > 1 ? 'pos' : (sh < 0 ? 'neg' : 'warn')),
                   'rf = 3.5%')}
          ${kpiCard('最大回撤', fmt(mdd, {pct:true}), mdd === null ? 'muted' : 'neg',
                   `历时 ${summary.max_drawdown_duration_days ?? '—'} 天`)}
        `;
        document.getElementById('nav-hint').textContent =
          `${summary.start_date || ''} → ${summary.end_date || ''} (${summary.n_days} 天)`;

        // NAV + ret 双图
        renderNavChart('nav-chart', navData);
        renderRetChart('ret-chart', navData);

        // 实例状态
        const instHtml = health.instances.filter(
          i => i.instance_id === getInstanceId()
        ).map(i => {
          const ret = i.latest_daily_return;
          const retCls = colorOf(ret);
          return `<tr>
            <td>${i.display_name || i.instance_id}${i.is_shadow ? ' ' + badge('shadow', 'warn') : ''}</td>
            <td class="num">${fmt(i.virtual_cash, {cur:true})}</td>
            <td class="num">${i.holdings_count}</td>
            <td class="num">${i.latest_nav ? fmt(i.latest_nav, {cur:true}) : '—'}</td>
            <td class="num ${retCls}">${ret === null ? '—' : fmt(ret, {pct:true, sign:true})}</td>
            <td><span style="font-size:0.78em;color:#8a93a0">${i.last_update || ''}</span></td>
          </tr>`;
        }).join('');
        document.getElementById('instance-state').innerHTML = `
          <table>
            <tr><th>Instance</th><th class="num">Cash</th><th class="num">持仓</th>
                <th class="num">NAV</th><th class="num">日收益</th><th>Last update</th></tr>
            ${instHtml}
          </table>
        `;
        renderPortfolioOverview(portfolio);
      } catch (e) {
        document.getElementById('kpis-overview').innerHTML = `<div class="error">${e.message}</div>`;
      }
    }

    function kpiCard(label, value, cls, sub) {
      return `<div class="kpi">
        <div class="kpi-label">${label}</div>
        <div class="kpi-value ${cls || ''}">${value}</div>
        <div class="kpi-sub">${sub || ''}</div>
      </div>`;
    }

    function renderNavChart(canvasId, navData, lineColor = '#40d6a0') {
      const items = navData.items.slice().reverse();
      const labels = items.map(i => i.date);
      const navs = items.map(i => i.nav);
      destroyChart(canvasId);
      charts[canvasId] = new Chart(document.getElementById(canvasId), {
        type: 'line',
        data: {
          labels,
          datasets: [{
            label: 'NAV', data: navs,
            borderColor: lineColor,
            backgroundColor: lineColor === '#51c8f2' ? 'rgba(81,200,242,0.08)' : 'rgba(64,214,160,0.08)',
            fill: true, tension: 0.2, pointRadius: 2, borderWidth: 2,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          scales: {
            y: { ticks: { color: '#8a93a0', callback: v => '¥' + (v/1e6).toFixed(2) + 'M' },
                 grid: { color: '#2a3340' } },
            x: { ticks: { color: '#8a93a0' }, grid: { display: false } },
          },
          plugins: { legend: { display: false } },
        },
      });
    }

    function renderRetChart(canvasId, navData) {
      const items = navData.items.slice().reverse();
      const labels = items.map(i => i.date);
      const rets = items.map(i => i.daily_return === null ? null : i.daily_return * 100);
      destroyChart(canvasId);
      charts[canvasId] = new Chart(document.getElementById(canvasId), {
        type: 'bar',
        data: {
          labels,
          datasets: [{
            label: '日收益 (%)', data: rets, borderWidth: 0,
            backgroundColor: rets.map(r => r === null ? '#555' : (r > 0 ? '#4ade80' : '#f87171')),
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          scales: {
            y: { ticks: { color: '#8a93a0', callback: v => v.toFixed(2) + '%' },
                 grid: { color: '#2a3340' } },
            x: { ticks: { color: '#8a93a0' }, grid: { display: false } },
          },
          plugins: { legend: { display: false } },
        },
      });
    }

    function destroyChart(id) {
      if (charts[id]) { charts[id].destroy(); delete charts[id]; }
    }

    // ── 收益分析 ────────────────────────────────────────────────
    async function renderReturns() {
      const period = getPeriod();
      try {
        const [summary, navData, weekly, monthly, yearly] = await Promise.all([
          api('/admin/metrics/summary?' + selectedQuery({period})),
          api(navQuery(period)),
          api('/admin/metrics/periodic?' + selectedQuery({period, freq: 'weekly'})),
          api('/admin/metrics/periodic?' + selectedQuery({period, freq: 'monthly'})),
          api('/admin/metrics/periodic?' + selectedQuery({period: 'all', freq: 'yearly'})),
        ]);

        document.getElementById('kpis-returns').innerHTML = `
          ${kpiCard('累计收益', fmt(summary.cumulative_return, {pct:true, sign:true}),
                   colorOf(summary.cumulative_return),
                   `期间 ${summary.n_days} 天`)}
          ${kpiCard('年化收益', fmt(summary.annualized_return, {pct:true, sign:true}),
                   colorOf(summary.annualized_return), '基于 252')}
          ${kpiCard('胜率', fmt(summary.win_rate, {pct:true}),
                   summary.win_rate > 0.5 ? 'pos' : 'muted',
                   `${Math.round((summary.win_rate || 0) * summary.n_days)} / ${summary.n_days}`)}
          ${kpiCard('Profit Factor', fmt(summary.profit_factor, {dec:2}),
                   summary.profit_factor > 1 ? 'pos' : (summary.profit_factor < 1 ? 'neg' : 'muted'),
                   '盈利和 / 亏损和')}
        `;
        document.getElementById('nav2-hint').textContent =
          `${summary.start_date || ''} → ${summary.end_date || ''}`;
        renderNavChart('nav-chart2', navData);

        document.getElementById('weekly-returns').innerHTML = renderPeriodTable(weekly.items, '周');
        document.getElementById('monthly-returns').innerHTML = renderPeriodTable(monthly.items, '月');
        document.getElementById('yearly-returns').innerHTML = renderPeriodTable(yearly.items, '年');
      } catch (e) {
        document.getElementById('kpis-returns').innerHTML = `<div class="error">${e.message}</div>`;
      }
    }

    function renderPeriodTable(items, unitLabel) {
      if (!items || items.length === 0) return `<p class="loading">无 ${unitLabel} 度数据</p>`;
      const recent = items.slice(-24).reverse();
      const html = recent.map(it => `
        <tr>
          <td>${it.period}</td>
          <td class="num">${it.n_days}</td>
          <td class="num">${fmt(it.nav_start, {curMM:true})}</td>
          <td class="num">${fmt(it.nav_end, {curMM:true})}</td>
          <td class="num ${colorOf(it.return)}">${fmt(it.return, {pct:true, sign:true})}</td>
          <td class="num ${colorOf(it.pnl)}">${fmt(it.pnl, {cur:true, sign:true})}</td>
        </tr>
      `).join('');
      return `<table>
        <tr><th>${unitLabel}期</th><th class="num">天</th><th class="num">起 NAV</th>
            <th class="num">终 NAV</th><th class="num">收益</th><th class="num">P&amp;L</th></tr>
        ${html}
      </table>`;
    }

    // ── 风险分析 ────────────────────────────────────────────────
    async function renderRisk() {
      const period = getPeriod();
      try {
        const [summary, dd, navData] = await Promise.all([
          api('/admin/metrics/summary?' + selectedQuery({period})),
          api('/admin/metrics/drawdown?' + selectedQuery({period})),
          api(navQuery(period)),
        ]);

        document.getElementById('kpis-risk').innerHTML = `
          ${kpiCard('Sharpe', fmt(summary.sharpe, {dec:2}),
                   summary.sharpe === null ? 'muted' : (summary.sharpe > 1 ? 'pos' : 'warn'),
                   'rf=3.5%')}
          ${kpiCard('Sortino', fmt(summary.sortino, {dec:2}),
                   summary.sortino === null ? 'muted' : (summary.sortino > 1 ? 'pos' : 'warn'),
                   '仅下行波动')}
          ${kpiCard('Calmar', fmt(summary.calmar, {dec:2}),
                   summary.calmar === null ? 'muted' : (summary.calmar > 0.5 ? 'pos' : 'warn'),
                   '年化 / |MDD|')}
          ${kpiCard('年化波动', fmt(summary.annualized_volatility, {pct:true}),
                   'muted', '标准差 ×√252')}
        `;

        // DD chart
        destroyChart('dd-chart');
        const ddVals = dd.drawdown.map(v => v * 100);
        charts['dd-chart'] = new Chart(document.getElementById('dd-chart'), {
          type: 'line',
          data: {
            labels: dd.dates,
            datasets: [{
              label: 'Drawdown (%)', data: ddVals,
              borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,0.2)',
              fill: true, tension: 0.2, pointRadius: 1, borderWidth: 2,
            }],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            scales: {
              y: { ticks: { color: '#8a93a0', callback: v => v.toFixed(1) + '%' },
                   grid: { color: '#2a3340' }, max: 0 },
              x: { ticks: { color: '#8a93a0' }, grid: { display: false } },
            },
            plugins: { legend: { display: false } },
          },
        });

        // 收益直方图
        const rets = navData.items.map(i => i.daily_return).filter(r => r !== null).map(r => r * 100);
        renderRetHistogram('ret-hist-chart', rets);

        // 风险摘要表
        document.getElementById('risk-summary').innerHTML = `
          <table>
            <tr><th>指标</th><th class="num">值</th></tr>
            <tr><td>最大回撤</td><td class="num neg">${fmt(summary.max_drawdown, {pct:true})}</td></tr>
            <tr><td>MDD 历时</td><td class="num">${summary.max_drawdown_duration_days ?? '—'} 天</td></tr>
            <tr><td>VaR 95% (1日)</td><td class="num neg">${fmt(summary.var_95, {pct:true})}</td></tr>
            <tr><td>平均盈利</td><td class="num pos">${fmt(summary.avg_win, {pct:true, sign:true})}</td></tr>
            <tr><td>平均亏损</td><td class="num neg">${fmt(summary.avg_loss, {pct:true, sign:true})}</td></tr>
            <tr><td>峰值 NAV</td><td class="num">${fmt(summary.peak_nav, {cur:true})}</td></tr>
          </table>
        `;
      } catch (e) {
        document.getElementById('kpis-risk').innerHTML = `<div class="error">${e.message}</div>`;
      }
    }

    function renderRetHistogram(canvasId, rets) {
      // 简单直方图：[-3, -2, -1, 0, 1, 2, 3]% 7 bins
      const bins = [-3, -2, -1, 0, 1, 2, 3];
      const counts = new Array(bins.length).fill(0);
      const labels = ['< -3%', '-3~-2%', '-2~-1%', '-1~0%', '0~1%', '1~2%', '2~3%', '> 3%'];
      const countsFull = new Array(labels.length).fill(0);
      rets.forEach(r => {
        if (r < -3) countsFull[0]++;
        else if (r < -2) countsFull[1]++;
        else if (r < -1) countsFull[2]++;
        else if (r < 0) countsFull[3]++;
        else if (r < 1) countsFull[4]++;
        else if (r < 2) countsFull[5]++;
        else if (r < 3) countsFull[6]++;
        else countsFull[7]++;
      });
      destroyChart(canvasId);
      charts[canvasId] = new Chart(document.getElementById(canvasId), {
        type: 'bar',
        data: {
          labels, datasets: [{
            label: '天数', data: countsFull, borderWidth: 0,
            backgroundColor: labels.map((_, i) => i < 4 ? '#f87171' : '#4ade80'),
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          scales: {
            y: { ticks: { color: '#8a93a0' }, grid: { color: '#2a3340' } },
            x: { ticks: { color: '#8a93a0' }, grid: { display: false } },
          },
          plugins: { legend: { display: false } },
        },
      });
    }

    // ── 策略内部 ────────────────────────────────────────────────
    async function renderStrategy() {
      try {
        if (isShadowSelected()) {
          const summary = await api('/admin/shadow/summary');
          const item = summary.items.find(i => i.shadow_id === getInstanceId());
          if (!item) throw new Error(`shadow instance not found: ${getInstanceId()}`);
          document.getElementById('strategy-state').innerHTML = `
            <table>
              <tr><th>Instance</th><th>状态</th><th>性质</th><th>Decision</th>
                  <th>As-of</th><th>Source version</th></tr>
              <tr><td>${item.shadow_id} ${badge('shadow', 'warn')}</td>
                  <td>${item.status}</td><td>仅虚拟账本、无订单</td>
                  <td>${item.decision_date || '—'}</td><td>${item.as_of_date || '—'}</td>
                  <td style="font-size:0.78em">${item.source_version || '—'}</td></tr>
            </table>`;
          document.getElementById('bl-total').textContent = '—';
          document.getElementById('blacklist').innerHTML =
            '<p class="loading">shadow 实例不进入实盘下单黑名单流程</p>';
          document.getElementById('recent-rejected').innerHTML =
            '<p class="loading">shadow 实例不会产生预检拒绝订单</p>';
          document.getElementById('bk-divergence').innerHTML =
            '<p style="color:#4ade80">✓ shadow 与订单/成交表物理隔离</p>';
          return;
        }
        const [state, bl, bk] = await Promise.all([
          api('/admin/strategy-state?' + selectedQuery()),
          api('/admin/blacklist?lookback_days=30'),
          api('/admin/bookkeeping-divergence'),
        ]);

        const stHtml = state.items.map(i => {
          const ss = i.strategy_state || {};
          const lastRb = ss.last_rb_idx;
          const nextRbDi = lastRb !== undefined && lastRb !== null ? lastRb + 42 : '—';
          const prev_hedge = ss.prev_hedge;
          const hist_n = (ss.daily_rets || []).length;
          return `<tr>
            <td>${i.instance_id}</td>
            <td class="num">${fmt(i.virtual_cash, {cur:true})}</td>
            <td class="num">${i.holdings_count}</td>
            <td class="num">${lastRb !== undefined && lastRb !== null ? lastRb : '—'}</td>
            <td class="num">${nextRbDi}</td>
            <td class="num">${prev_hedge !== undefined && prev_hedge !== null ? fmt(prev_hedge, {pct:true}) : '—'}</td>
            <td class="num">${hist_n}</td>
            <td><span style="font-size:0.78em;color:#8a93a0">${i.last_update || ''}</span></td>
          </tr>`;
        }).join('');
        document.getElementById('strategy-state').innerHTML = `
          <table>
            <tr><th>Instance</th><th class="num">Cash</th><th class="num">持仓</th>
                <th class="num">last_rb_idx</th><th class="num">next_rb_di</th>
                <th class="num">prev_hedge</th><th class="num">history len</th>
                <th>Last update</th></tr>
            ${stHtml}
          </table>
          <p style="font-size:0.8em;color:#8a93a0;margin-top:8px">
            ℹ️ 每 42 个交易日主调仓一次。<b>next_rb_di</b> = last_rb_idx + 42。
            <b>prev_hedge</b> = V20H IM 期货空头比例（Phase 14c 实盘 skip）。
          </p>
        `;

        // 黑名单
        document.getElementById('bl-total').textContent = bl.merged_total;
        const blManual = bl.manual_entries.slice(0, 15).map(e =>
          `<tr><td>${e.symbol}</td><td style="font-size:0.78em;color:#8a93a0">${e.reason || '—'}</td></tr>`
        ).join('');
        document.getElementById('blacklist').innerHTML = `
          <p style="font-size:0.85em;color:#8a93a0;margin-bottom:8px">
            自动 ${bl.auto_unique_symbols} + 手工 ${bl.manual_count} = ${bl.merged_total}
            ${bl.merged_total > 100 ? badge('⚠️ 超 100', 'danger') : ''}
          </p>
          <table>
            <tr><th>Symbol</th><th>Reason</th></tr>
            ${blManual}
          </table>
        `;

        // Recent rejected
        const autoEntries = Object.entries(bl.auto_by_symbol).sort((a, b) => b[1] - a[1]).slice(0, 10);
        document.getElementById('recent-rejected').innerHTML = `
          <table>
            <tr><th>Symbol</th><th class="num">REJECTED 次数</th></tr>
            ${autoEntries.map(([s, c]) => `<tr><td>${s}</td><td class="num">${c}</td></tr>`).join('')}
          </table>
        `;

        // Bookkeeping divergence
        if (bk.count === 0) {
          document.getElementById('bk-divergence').innerHTML =
            `<p style="color:#4ade80">✓ 暂无分叉，账本正常</p>`;
        } else {
          const rows = bk.items.map(o => `<tr>
            <td>${o.valid_date}</td><td>${o.symbol}</td><td>${o.direction}</td>
            <td class="num">${o.quantity}</td><td>${o.status}</td>
            <td style="font-size:0.78em;color:#8a93a0">${o.order_id}</td>
          </tr>`).join('');
          document.getElementById('bk-divergence').innerHTML = `
            <p class="error">⚠️ 真账户已 FILL 但虚拟账本对账失败 ${bk.count} 单，须人工对账</p>
            <table>
              <tr><th>Date</th><th>Symbol</th><th>Dir</th><th class="num">Qty</th>
                  <th>Status</th><th>Order ID</th></tr>
              ${rows}
            </table>
          `;
        }
      } catch (e) {
        document.getElementById('strategy-state').innerHTML = `<div class="error">${e.message}</div>`;
      }
    }

    // ── 交易分析 ────────────────────────────────────────────────
    async function renderTrades() {
      const period = getPeriod();
      try {
        if (isShadowSelected()) {
          document.getElementById('kpis-trades').innerHTML =
            `<div class="card wide"><p style="color:#93c5fd">
              ${getInstanceId()} 是仅虚拟记账的 shadow 实例，不产生任何订单或成交。
            </p></div>`;
          document.getElementById('orders-matrix').innerHTML =
            '<p class="loading">无订单：shadow 边界已启用</p>';
          return;
        }
        const [trade, ordSummary] = await Promise.all([
          api('/admin/metrics/trade-analytics?period=' + period),
          api('/admin/orders-summary?days=' + ({7:7, '7d':7, '30d':30, '90d':90, '180d':180, ytd:365, '1y':365, all:365}[period] || 30)),
        ]);

        const fillBadge = trade.fill_rate === null ? 'muted' :
                         (trade.fill_rate > 0.9 ? 'pos' : (trade.fill_rate > 0.7 ? 'warn' : 'neg'));
        document.getElementById('kpis-trades').innerHTML = `
          ${kpiCard('订单总数', trade.n_orders, 'muted', `期间 ${period}`)}
          ${kpiCard('Fill Rate', fmt(trade.fill_rate, {pct:true}), fillBadge,
                   'FILLED+PARTIAL / 总')}
          ${kpiCard('成交金额', fmt(trade.total_filled_amount, {curMM:true}), 'muted',
                   `${trade.n_trades} 笔成交`)}
          ${kpiCard('对账分叉', trade.bookkeeping_divergence_count,
                   trade.bookkeeping_divergence_count === 0 ? 'pos' : 'neg',
                   '需要人工对账')}
        `;

        // Orders matrix
        const dates = Object.keys(ordSummary.by_date).sort();
        if (dates.length === 0) {
          document.getElementById('orders-matrix').innerHTML = '<p class="loading">无订单数据</p>';
          return;
        }
        const allStatuses = ['PENDING', 'FILLED', 'PARTIAL', 'CANCELLED', 'REJECTED'];
        let html = '<table><tr><th>Date</th><th>Group</th><th>Dir</th>';
        allStatuses.forEach(s => html += `<th class="num">${s}</th>`);
        html += '<th class="num">Total</th></tr>';
        dates.forEach(date => {
          Object.entries(ordSummary.by_date[date]).forEach(([ag, dirs]) => {
            Object.entries(dirs).forEach(([direction, statuses]) => {
              const total = Object.values(statuses).reduce((a, b) => a + b, 0);
              html += `<tr><td>${date}</td><td>${ag}</td><td>${direction}</td>`;
              allStatuses.forEach(s => {
                const n = statuses[s] || 0;
                html += `<td class="num">${n > 0 ? n : ''}</td>`;
              });
              html += `<td class="num"><b>${total}</b></td></tr>`;
            });
          });
        });
        html += '</table>';
        document.getElementById('orders-matrix').innerHTML = html;
      } catch (e) {
        document.getElementById('kpis-trades').innerHTML = `<div class="error">${e.message}</div>`;
      }
    }

    // ── 运营与对账 ──────────────────────────────────────────────
    async function loadOps(){
      const inst = getInstanceId();
      const [al, runs, integ, anom] = await Promise.all([
        api('/admin/alerts'),
        api('/admin/ops/pipeline-runs?days=14'),
        api('/admin/ops/snapshot-integrity?instance_id='+inst+'&lookback=30'),
        api('/admin/ops/reconcile-anomalies?instance_id='+inst),
      ]);
      // Alert feed (sort critical>warn>info)
      const sev={critical:0,warn:1,info:2};
      const alerts=(al.alerts||[]).slice().sort((a,b)=>sev[a.severity]-sev[b.severity]);
      document.getElementById('ops-alerts').innerHTML = alerts.length
        ? alerts.map(a=>`<div class="${a.severity==='critical'?'crit':(a.severity==='warn'?'stale':'')}">
            <b>[${a.severity}]</b> ${a.message} <span style="color:#8a93a0">· ${a.as_of}</span></div>`).join('')
        : '<div style="color:#4ade80">✓ 无告警</div>';
      // Pipeline runs table
      document.getElementById('ops-runs').innerHTML =
        '<table><tr><th>valid_date</th><th>status</th><th>signal_time</th><th class="num">orders</th></tr>'+
        (runs.runs||[]).map(r=>`<tr><td>${r.valid_date}</td>
          <td><span class="${r.status==='missing'?'crit':(r.status==='ok'?'':'')}">${r.status}</span></td>
          <td>${r.signal_time||'—'}</td><td class="num">${r.orders}</td></tr>`).join('')+'</table>';
      // Freshness (from META if present)
      const fr=(window.META&&window.META.freshness)||{};
      document.getElementById('ops-freshness').innerHTML =
        `行情 latest: <span class="${staleClass(fr.market_lag_days)}">${fr.market_latest||'—'}`+
        `${fr.market_lag_days!=null?` (${fr.market_lag_days}d)`:''}</span> · probe ${fr.probe||'—'}`;
      // Snapshot integrity
      const iss=integ.issues||[];
      document.getElementById('ops-integrity').innerHTML = iss.length
        ? iss.map(i=>`<div class="stale">⚠ ${i.type} @ ${i.date} — ${i.detail||''}</div>`).join('')
        : '<div style="color:#4ade80">✓ 无冻结/缺口</div>';
      // Reconcile anomalies
      const an=anom.overnight_position_anomalies||[];
      document.getElementById('ops-anomalies').innerHTML = an.length
        ? '<table><tr><th>symbol</th><th class="num">prev</th><th class="num">cur</th><th class="num">×</th><th>window</th></tr>'+
          an.map(a=>`<tr class="crit"><td>${a.symbol}</td><td class="num">${fmtNum(a.prev_qty)}</td>
            <td class="num">${fmtNum(a.cur_qty)}</td><td class="num">${a.ratio??'∞'}</td>
            <td>${a.from_date}→${a.to_date}</td></tr>`).join('')+'</table>'
        : '<div style="color:#4ade80">✓ 无隔夜异常</div>';
    }

    // ── 主刷新 ─────────────────────────────────────────────────
    async function refreshAll() {
      if (!checkAuth()) return;
      document.getElementById('meta').textContent =
        `fetching... ${new Date().toLocaleString()} | period=${getPeriod()}`;
      try {
        if (currentTab === 'live') await renderLive();
        else if (currentTab === 'overview') await renderOverview();
        else if (currentTab === 'returns') await renderReturns();
        else if (currentTab === 'risk') await renderRisk();
        else if (currentTab === 'strategy') await renderStrategy();
        else if (currentTab === 'trades') await renderTrades();
        else if (currentTab === 'ops') await loadOps();
        document.getElementById('meta').textContent =
          `last refresh ${new Date().toLocaleString()} | tab=${currentTab} | period=${getPeriod()}`;
      } catch (e) {
        document.getElementById('meta').textContent = `error: ${e.message}`;
      }
    }

    async function initializeDashboard() {
      try {
        await loadInstanceOptions();
      } catch (e) {
        document.getElementById('meta').textContent = `instance load error: ${e.message}`;
        return;
      }
      await refreshAll();
      metaPoll();
      setInterval(metaPoll, 15000);
      setInterval(refreshAll, 30000);
    }

    function renderPortfolioOverview(data) {
      const rows = data.items.map(i => `<tr>
        <td>${i.display_name || i.instance_id}${i.is_shadow ? ' ' + badge('shadow', 'warn') : ''}</td>
        <td class="num">${fmt(i.virtual_cash, {cur:true})}</td>
        <td class="num">${i.holdings_count}</td>
        <td class="num">${fmt(i.latest_nav, {cur:true})}</td>
        <td class="num ${colorOf(i.latest_daily_return)}">${fmt(i.latest_daily_return, {pct:true, sign:true})}</td>
        <td>${i.latest_nav_date || '—'}</td>
      </tr>`).join('');
      document.getElementById('portfolio-overview').innerHTML = `<table>
        <tr><th>Instance</th><th class="num">Cash</th><th class="num">持仓</th>
            <th class="num">虚拟 NAV</th><th class="num">日收益</th><th>快照日</th></tr>
        ${rows || '<tr><td colspan="6" class="loading">暂无实例账本</td></tr>'}
      </table>`;
    }

    if (!checkAuth()) {
      document.getElementById('api-key-input').focus();
    } else {
      initializeDashboard();
    }
  </script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    """单 HTML，纯前端渲染。"""
    return HTMLResponse(content=_DASHBOARD_HTML)
