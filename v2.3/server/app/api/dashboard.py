"""GET /dashboard — 极轻量纯 HTML 可视化面板。

设计：
- 一个 HTML 字符串，~30KB，没任何 server-side 计算
- Chart.js / 简洁 CSS 都从浏览器 CDN 加载，服务器零额外内存
- 用户首次访问：弹窗输入 API key，存 localStorage
- JS 调现有的 /admin/health /admin/nav-history /admin/orders-summary /admin/blacklist
- 自动 60s 刷新
- 整个端点的内存开销 < 1 MB
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
  <title>V20H Pipeline Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, "PingFang HK", "Microsoft YaHei", sans-serif;
      background: #0f1419;
      color: #d4d8de;
      padding: 16px;
      min-height: 100vh;
    }
    .header {
      display: flex; justify-content: space-between; align-items: center;
      padding: 12px 20px; background: #1a2027; border-radius: 8px;
      margin-bottom: 16px; border-left: 4px solid #4ea1ff;
    }
    .header h1 { font-size: 1.3em; color: #fff; font-weight: 600; }
    .header .meta { font-size: 0.85em; color: #8a93a0; }
    .refresh-btn {
      background: #4ea1ff; color: #fff; border: none;
      padding: 6px 14px; border-radius: 6px; cursor: pointer;
      font-size: 0.9em; font-weight: 500;
    }
    .refresh-btn:hover { background: #6db4ff; }
    .refresh-btn:disabled { background: #555; cursor: not-allowed; }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
      gap: 16px;
    }
    .card {
      background: #1a2027; border-radius: 8px; padding: 16px;
      border: 1px solid #2a3340;
    }
    .card h2 {
      font-size: 1em; color: #4ea1ff; margin-bottom: 12px;
      padding-bottom: 8px; border-bottom: 1px solid #2a3340;
      font-weight: 600;
    }
    .card.wide { grid-column: 1 / -1; }
    .card.tall { min-height: 320px; }

    .kpi-grid {
      display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px;
    }
    .kpi {
      background: #0f1419; padding: 12px; border-radius: 6px;
      border-left: 3px solid #4ea1ff;
    }
    .kpi-label { font-size: 0.75em; color: #8a93a0; text-transform: uppercase; }
    .kpi-value { font-size: 1.4em; font-weight: 600; color: #fff; margin-top: 4px; }
    .kpi-value.pos { color: #4ade80; }
    .kpi-value.neg { color: #f87171; }
    .kpi-value.warn { color: #fbbf24; }
    .kpi-sub { font-size: 0.75em; color: #8a93a0; margin-top: 2px; }

    table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
    table th, table td {
      padding: 6px 8px; text-align: left;
      border-bottom: 1px solid #2a3340;
    }
    table th { color: #8a93a0; font-weight: 500; }
    table tr:hover { background: #0f1419; }
    .num { font-family: 'SF Mono', Monaco, monospace; text-align: right; }
    .badge {
      display: inline-block; padding: 1px 6px; border-radius: 3px;
      font-size: 0.75em; font-weight: 500;
    }
    .badge-success { background: #064e3b; color: #4ade80; }
    .badge-warn { background: #422006; color: #fbbf24; }
    .badge-danger { background: #4c0519; color: #f87171; }
    .badge-info { background: #1e3a5f; color: #93c5fd; }

    .loading { color: #8a93a0; font-style: italic; padding: 20px; text-align: center; }
    .error { color: #f87171; background: #2a0a0a; padding: 12px; border-radius: 6px; }

    .chart-container { position: relative; height: 240px; }

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
  <!-- Login Modal -->
  <div id="login-modal">
    <div class="modal-box">
      <h3>🔐 V20H Dashboard</h3>
      <p style="color:#8a93a0; font-size:0.9em; margin-bottom:12px;">
        请输入 API Key（仅存浏览器 localStorage，不上传）:
      </p>
      <input type="password" id="api-key-input"
             placeholder="pipeline-v23-shared-secret-2026"
             onkeypress="if(event.key==='Enter') saveKey()">
      <button onclick="saveKey()">登录</button>
    </div>
  </div>

  <!-- Header -->
  <div class="header">
    <div>
      <h1>📡 V20H Pipeline Dashboard</h1>
      <div class="meta" id="meta">Loading...</div>
    </div>
    <button class="refresh-btn" id="refresh-btn" onclick="refreshAll()">↻ Refresh</button>
  </div>

  <!-- Cards -->
  <div class="grid">
    <!-- 1. Health KPIs -->
    <div class="card">
      <h2>① 系统健康度</h2>
      <div class="kpi-grid" id="health-kpis"><div class="loading">Loading...</div></div>
    </div>

    <!-- 2. NAV chart -->
    <div class="card tall">
      <h2>② NAV trajectory (paper_v20h)</h2>
      <div class="chart-container"><canvas id="nav-chart"></canvas></div>
    </div>

    <!-- 3. Daily Return chart -->
    <div class="card tall">
      <h2>③ 每日收益 (%)</h2>
      <div class="chart-container"><canvas id="ret-chart"></canvas></div>
    </div>

    <!-- 4. Instance state -->
    <div class="card">
      <h2>④ 策略实例状态</h2>
      <div id="instance-state"><div class="loading">Loading...</div></div>
    </div>

    <!-- 5. Orders matrix (wide) -->
    <div class="card wide">
      <h2>⑤ 最近 14 天 Orders 状态矩阵</h2>
      <div id="orders-matrix"><div class="loading">Loading...</div></div>
    </div>

    <!-- 6. Blacklist -->
    <div class="card">
      <h2>⑥ 黑名单 (合计 <span id="bl-total">-</span>)</h2>
      <div id="blacklist"><div class="loading">Loading...</div></div>
    </div>

    <!-- 7. Recent rejected -->
    <div class="card">
      <h2>⑦ 最近 REJECTED Top 10</h2>
      <div id="recent-rejected"><div class="loading">Loading...</div></div>
    </div>
  </div>

  <script>
    const API_BASE = window.location.origin;
    let API_KEY = localStorage.getItem('qmt_api_key') || '';
    let navChart = null, retChart = null;

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
      if (n === null || n === undefined) return '—';
      const sign = opts.sign && n > 0 ? '+' : '';
      if (opts.pct) return sign + (n * 100).toFixed(2) + '%';
      if (opts.cur) return '¥' + n.toLocaleString('en-US', {maximumFractionDigits: 0});
      if (opts.dec) return sign + n.toFixed(opts.dec);
      return sign + n.toString();
    }

    function badge(text, type) {
      return `<span class="badge badge-${type}">${text}</span>`;
    }

    async function renderHealth() {
      try {
        const d = await api('/admin/health');
        const ps = d.pred_status || {};
        const lag = ps.lag_hours;
        const lagBadge = lag === null ? '?' :
          lag < 24 ? badge('Fresh', 'success') :
          lag < 72 ? badge(lag.toFixed(0) + 'h', 'warn') :
          badge(lag.toFixed(0) + 'h ⚠️', 'danger');

        const bl = d.blacklist;
        const pending = d.pending_orders_total;
        const pendingBadge = pending === 0 ? badge('0', 'success') :
                            pending < 100 ? badge(pending, 'warn') :
                            badge(pending + ' ⚠️', 'danger');

        document.getElementById('health-kpis').innerHTML = `
          <div class="kpi">
            <div class="kpi-label">Pred 新鲜度</div>
            <div class="kpi-value">${lagBadge}</div>
            <div class="kpi-sub">${ps.size_mb || '?'} MB, ${ps.mtime || '?'}</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">Pending Orders</div>
            <div class="kpi-value">${pendingBadge}</div>
            <div class="kpi-sub">待 client 处理</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">黑名单</div>
            <div class="kpi-value">${bl.merged_total}</div>
            <div class="kpi-sub">自动 ${bl.auto_unique} + 手工 ${bl.manual_count}</div>
          </div>
          <div class="kpi">
            <div class="kpi-label">实例数</div>
            <div class="kpi-value">${d.instances.length}</div>
            <div class="kpi-sub">${d.instances.filter(i => i.holdings_count > 0).length} 个有持仓</div>
          </div>
        `;

        // Instance state
        const instHtml = d.instances.map(i => {
          const ret = i.latest_daily_return;
          const retClass = ret === null ? '' : (ret > 0 ? 'pos' : (ret < 0 ? 'neg' : ''));
          const retStr = ret === null ? '—' : fmt(ret, {pct: true, sign: true});
          return `<tr>
            <td>${i.instance_id}</td>
            <td class="num">${fmt(i.virtual_cash, {cur: true})}</td>
            <td class="num">${i.holdings_count}</td>
            <td class="num">${i.latest_nav ? fmt(i.latest_nav, {cur: true}) : '—'}</td>
            <td class="num ${retClass}">${retStr}</td>
          </tr>`;
        }).join('');
        document.getElementById('instance-state').innerHTML = `
          <table>
            <tr><th>Instance</th><th class="num">Cash</th><th class="num">持仓数</th>
                <th class="num">NAV</th><th class="num">日收益</th></tr>
            ${instHtml}
          </table>
        `;
      } catch (e) {
        document.getElementById('health-kpis').innerHTML =
          `<div class="error">${e.message}</div>`;
      }
    }

    async function renderNav() {
      try {
        const d = await api('/admin/nav-history?instance_id=paper_v20h_v20h_v1_3&limit=30');
        const items = d.items.slice().reverse(); // 升序
        const labels = items.map(i => i.date);
        const navs = items.map(i => i.nav);
        const rets = items.map(i => i.daily_return === null ? null : i.daily_return * 100);

        // NAV chart
        if (navChart) navChart.destroy();
        navChart = new Chart(document.getElementById('nav-chart'), {
          type: 'line',
          data: {
            labels,
            datasets: [{
              label: 'NAV',
              data: navs,
              borderColor: '#4ade80',
              backgroundColor: 'rgba(74, 222, 128, 0.1)',
              fill: true,
              tension: 0.2,
              pointRadius: 3,
              borderWidth: 2,
            }],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            scales: {
              y: { ticks: { color: '#8a93a0', callback: v => '¥' + (v/1e6).toFixed(2) + 'M' }, grid: { color: '#2a3340' } },
              x: { ticks: { color: '#8a93a0' }, grid: { display: false } },
            },
            plugins: { legend: { display: false } },
          },
        });

        // Daily return chart
        if (retChart) retChart.destroy();
        retChart = new Chart(document.getElementById('ret-chart'), {
          type: 'bar',
          data: {
            labels,
            datasets: [{
              label: '日收益 (%)',
              data: rets,
              backgroundColor: rets.map(r => r === null ? '#555' : (r > 0 ? '#4ade80' : '#f87171')),
              borderWidth: 0,
            }],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            scales: {
              y: { ticks: { color: '#8a93a0', callback: v => v.toFixed(2) + '%' }, grid: { color: '#2a3340' } },
              x: { ticks: { color: '#8a93a0' }, grid: { display: false } },
            },
            plugins: { legend: { display: false } },
          },
        });
      } catch (e) {
        console.error('renderNav', e);
      }
    }

    async function renderOrdersMatrix() {
      try {
        const d = await api('/admin/orders-summary?days=14');
        const dates = Object.keys(d.by_date).sort();
        if (dates.length === 0) {
          document.getElementById('orders-matrix').innerHTML = '<p class="loading">No orders</p>';
          return;
        }
        let html = '<table><tr><th>Date</th><th>Group</th><th>Dir</th>';
        const allStatuses = ['PENDING', 'FILLED', 'PARTIAL', 'CANCELLED', 'REJECTED'];
        allStatuses.forEach(s => html += `<th class="num">${s}</th>`);
        html += '<th class="num">Total</th></tr>';

        dates.forEach(date => {
          Object.entries(d.by_date[date]).forEach(([ag, dirs]) => {
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
        document.getElementById('orders-matrix').innerHTML = `<div class="error">${e.message}</div>`;
      }
    }

    async function renderBlacklist() {
      try {
        const d = await api('/admin/blacklist?lookback_days=30');
        document.getElementById('bl-total').textContent = d.merged_total;

        const manualHtml = d.manual_entries.slice(0, 30).map(e => `
          <tr><td>${e.symbol}</td>
              <td style="font-size:0.8em;color:#8a93a0">${e.reason || '—'}</td></tr>
        `).join('');
        document.getElementById('blacklist').innerHTML = `
          <p style="font-size:0.85em;color:#8a93a0;margin-bottom:8px">
            自动派生 ${d.auto_unique_symbols} + 手工 ${d.manual_count} = ${d.merged_total}
          </p>
          <table><tr><th>Symbol</th><th>Reason</th></tr>${manualHtml}</table>
        `;

        // Recent rejected top 10
        const autoEntries = Object.entries(d.auto_by_symbol).sort((a, b) => b[1] - a[1]).slice(0, 10);
        document.getElementById('recent-rejected').innerHTML = `
          <table><tr><th>Symbol</th><th class="num">REJECTED 次数</th></tr>
            ${autoEntries.map(([s, c]) => `<tr><td>${s}</td><td class="num">${c}</td></tr>`).join('')}
          </table>
        `;
      } catch (e) {
        document.getElementById('blacklist').innerHTML = `<div class="error">${e.message}</div>`;
      }
    }

    async function refreshAll() {
      if (!checkAuth()) return;
      const btn = document.getElementById('refresh-btn');
      btn.disabled = true; btn.textContent = '⟳ Refreshing...';
      document.getElementById('meta').textContent = 'fetching... ' + new Date().toLocaleString();

      await Promise.all([
        renderHealth(),
        renderNav(),
        renderOrdersMatrix(),
        renderBlacklist(),
      ]);

      document.getElementById('meta').textContent = 'last refresh ' + new Date().toLocaleString();
      btn.disabled = false; btn.textContent = '↻ Refresh';
    }

    // 启动
    if (!checkAuth()) {
      document.getElementById('api-key-input').focus();
    } else {
      refreshAll();
      // 自动 60s 刷新
      setInterval(refreshAll, 60000);
    }
  </script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    return HTMLResponse(content=_DASHBOARD_HTML)
