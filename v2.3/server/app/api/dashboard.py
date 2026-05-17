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
  <title>V20H Quant Dashboard</title>
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
  </style>
</head>
<body>
  <div id="login-modal">
    <div class="modal-box">
      <h3>🔐 V20H Quant Dashboard</h3>
      <p style="color:#8a93a0; font-size:0.9em; margin-bottom:12px;">
        请输入 API Key（仅存浏览器 localStorage）:
      </p>
      <input type="password" id="api-key-input"
             placeholder="pipeline-v23-shared-secret-2026"
             onkeypress="if(event.key==='Enter') saveKey()">
      <button onclick="saveKey()">登录</button>
    </div>
  </div>

  <div class="header">
    <div>
      <h1>📊 V20H Quant Dashboard</h1>
      <div class="meta" id="meta">Loading...</div>
    </div>
    <div class="toolbar">
      <label style="font-size:0.85em;color:#8a93a0;">期间:</label>
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

  <div class="tabs">
    <div class="tab active" data-view="overview" onclick="showTab('overview')">
      <span class="icon">📌</span>概览
    </div>
    <div class="tab" data-view="returns" onclick="showTab('returns')">
      <span class="icon">📈</span>收益分析
    </div>
    <div class="tab" data-view="risk" onclick="showTab('risk')">
      <span class="icon">📉</span>风险分析
    </div>
    <div class="tab" data-view="strategy" onclick="showTab('strategy')">
      <span class="icon">⚙️</span>策略内部
    </div>
    <div class="tab" data-view="trades" onclick="showTab('trades')">
      <span class="icon">💼</span>交易分析
    </div>
  </div>

  <!-- 概览 -->
  <div class="view active" id="view-overview">
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

  <script>
    const API_BASE = window.location.origin;
    let API_KEY = localStorage.getItem('qmt_api_key') || '';
    let currentTab = 'overview';
    let charts = {};

    function saveKey() {
      const k = document.getElementById('api-key-input').value.trim();
      if (!k) return;
      localStorage.setItem('qmt_api_key', k);
      API_KEY = k;
      document.getElementById('login-modal').style.display = 'none';
      refreshAll();
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

    function fmt(n, opts = {}) {
      if (n === null || n === undefined || (typeof n === 'number' && isNaN(n))) return '—';
      const sign = opts.sign && n > 0 ? '+' : '';
      if (opts.pct) return sign + (n * 100).toFixed(2) + '%';
      if (opts.bps) return sign + (n * 10000).toFixed(0) + 'bps';
      if (opts.cur) return '¥' + n.toLocaleString('en-US', {maximumFractionDigits: 0});
      if (opts.curMM) return '¥' + (n/1e6).toFixed(2) + 'M';
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

    function onPeriodChange() {
      refreshAll();
    }

    function showTab(name) {
      currentTab = name;
      document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === name));
      document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
      refreshAll();
    }

    // ── 概览 ────────────────────────────────────────────────────
    async function renderOverview() {
      const period = getPeriod();
      try {
        const [health, summary, navData] = await Promise.all([
          api('/admin/health'),
          api('/admin/metrics/summary?period=' + period),
          api('/admin/nav-history?instance_id=paper_v20h_v20h_v1_3&limit=300'),
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
        const instHtml = health.instances.map(i => {
          const ret = i.latest_daily_return;
          const retCls = colorOf(ret);
          return `<tr>
            <td>${i.instance_id}</td>
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

    function renderNavChart(canvasId, navData) {
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
            borderColor: '#4ade80', backgroundColor: 'rgba(74,222,128,0.1)',
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
          api('/admin/metrics/summary?period=' + period),
          api('/admin/nav-history?instance_id=paper_v20h_v20h_v1_3&limit=300'),
          api('/admin/metrics/periodic?period=' + period + '&freq=weekly'),
          api('/admin/metrics/periodic?period=' + period + '&freq=monthly'),
          api('/admin/metrics/periodic?period=all&freq=yearly'),
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
          api('/admin/metrics/summary?period=' + period),
          api('/admin/metrics/drawdown?period=' + period),
          api('/admin/nav-history?instance_id=paper_v20h_v20h_v1_3&limit=300'),
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
        const [state, bl, bk] = await Promise.all([
          api('/admin/strategy-state'),
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

    // ── 主刷新 ─────────────────────────────────────────────────
    async function refreshAll() {
      if (!checkAuth()) return;
      document.getElementById('meta').textContent =
        `fetching... ${new Date().toLocaleString()} | period=${getPeriod()}`;
      try {
        if (currentTab === 'overview') await renderOverview();
        else if (currentTab === 'returns') await renderReturns();
        else if (currentTab === 'risk') await renderRisk();
        else if (currentTab === 'strategy') await renderStrategy();
        else if (currentTab === 'trades') await renderTrades();
        document.getElementById('meta').textContent =
          `last refresh ${new Date().toLocaleString()} | tab=${currentTab} | period=${getPeriod()}`;
      } catch (e) {
        document.getElementById('meta').textContent = `error: ${e.message}`;
      }
    }

    if (!checkAuth()) {
      document.getElementById('api-key-input').focus();
    } else {
      refreshAll();
      setInterval(refreshAll, 60000);
    }
  </script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    """单 HTML，纯前端渲染。"""
    return HTMLResponse(content=_DASHBOARD_HTML)
