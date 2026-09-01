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
    .trajectory-card { min-height: 430px; padding: 0; overflow: hidden; }
    .trajectory-header {
      min-height: 65px; padding: 14px 16px 12px; display: flex; justify-content: space-between;
      align-items: center; gap: 16px; border-bottom: 1px solid rgba(135,157,184,.13);
      background: linear-gradient(180deg, rgba(19,29,40,.86), rgba(14,20,28,.35));
    }
    .trajectory-title-wrap { min-width: 0; display: flex; align-items: center; gap: 11px; }
    .trajectory-glyph {
      position: relative; flex: 0 0 32px; width: 32px; height: 32px; border-radius: 8px;
      border: 1px solid rgba(81,200,242,.26); overflow: hidden;
      background:
        linear-gradient(rgba(81,200,242,.07) 1px, transparent 1px) 0 0 / 100% 8px,
        linear-gradient(90deg, rgba(81,200,242,.07) 1px, transparent 1px) 0 0 / 8px 100%,
        #0a121a;
    }
    .trajectory-glyph::before {
      content: ""; position: absolute; width: 27px; height: 2px; left: 3px; top: 17px;
      border-radius: 2px; transform: rotate(-24deg);
      background: linear-gradient(90deg, #3e7790 0 22%, var(--accent) 23% 67%, #d9f7ff 68%);
      box-shadow: 0 0 9px rgba(81,200,242,.45);
    }
    .trajectory-glyph::after {
      content: ""; position: absolute; width: 4px; height: 4px; right: 3px; top: 9px;
      border-radius: 50%; background: #e5faff; box-shadow: 0 0 8px var(--accent);
    }
    .trajectory-title h2 {
      min-height: 0; margin: 0; padding: 0; border: 0; color: #edf6ff;
      font: 660 12px/1.2 var(--mono); letter-spacing: .065em;
    }
    .trajectory-title p { margin-top: 5px; color: var(--muted); font: 8px/1.35 var(--mono); }
    .trajectory-actions { display: flex; align-items: center; gap: 8px; }
    .trajectory-modes button { min-width: 54px; }
    .trajectory-benchmark {
      height: 31px; padding: 0 26px 0 9px; border: 1px solid #263442; border-radius: 7px;
      color: #aebfce; background: #090e14; font: 8px var(--mono); letter-spacing: .03em;
      cursor: pointer;
    }
    .trajectory-benchmark:focus { outline: 1px solid rgba(81,200,242,.45); outline-offset: 1px; }
    .trajectory-segmented {
      display: inline-flex; padding: 3px; gap: 2px; border: 1px solid #263442;
      border-radius: 7px; background: #090e14;
    }
    .trajectory-segmented button {
      min-width: 38px; height: 24px; padding: 0 8px; border: 0; border-radius: 4px;
      color: #66778a; background: transparent; font: 8px var(--mono); cursor: pointer;
    }
    .trajectory-segmented button:hover { color: #c7d7e7; background: #121c27; }
    .trajectory-segmented button.active {
      color: #dff8ff; background: rgba(81,200,242,.13); box-shadow: inset 0 0 0 1px rgba(81,200,242,.22);
    }
    .trajectory-context {
      min-height: 32px; padding: 0 16px; display: flex; align-items: center; gap: 13px;
      border-bottom: 1px solid rgba(135,157,184,.09); color: #617286; font: 8px var(--mono);
      white-space: nowrap; overflow-x: auto; scrollbar-width: none;
    }
    .trajectory-context::-webkit-scrollbar { display: none; }
    .trajectory-context .observed { color: var(--positive); }
    .trajectory-context .missing { color: #8a6d48; }
    .trajectory-feed { display: inline-flex; align-items: center; gap: 6px; }
    .trajectory-feed i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
    .trajectory-stats {
      display: grid; grid-template-columns: repeat(6, minmax(0,1fr)); padding: 12px 16px 10px;
      border-bottom: 1px solid rgba(135,157,184,.09);
    }
    .trajectory-stat { min-width: 0; padding: 0 12px; border-left: 1px solid rgba(135,157,184,.1); }
    .trajectory-stat:first-child { padding-left: 0; border-left: 0; }
    .trajectory-stat-label { color: #617286; font: 8px/1.2 var(--mono); letter-spacing: .05em; }
    .trajectory-stat-value {
      margin-top: 6px; color: #dce8f4; font: 13px/1 var(--mono); letter-spacing: -.035em;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .trajectory-stat-value.pos { color: var(--positive); }
    .trajectory-stat-value.neg { color: var(--negative); }
    .trajectory-chart { height: 268px; padding: 12px 12px 4px 6px; cursor: crosshair; }
    .trajectory-footer {
      min-height: 31px; display: flex; justify-content: space-between; align-items: center; gap: 12px;
      padding: 0 16px; border-top: 1px solid rgba(135,157,184,.09); color: #566679; font: 8px var(--mono);
    }
    .trajectory-legend { display: inline-flex; align-items: center; gap: 13px; }
    .trajectory-legend span { display: inline-flex; align-items: center; gap: 6px; }
    .trajectory-legend i { display: inline-block; width: 14px; height: 2px; background: var(--accent); }
    .trajectory-legend i.hwm { height: 1px; opacity: .55; background: repeating-linear-gradient(90deg,#8092a6 0 3px,transparent 3px 5px); }
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
    #execution-price-table { overflow-x: auto; }
    #execution-price-table table { min-width: 980px; }
    .execution-method {
      margin: -3px 0 12px; padding: 8px 10px; border: 1px solid rgba(81,200,242,.13);
      border-radius: 6px; color: #718398; background: rgba(81,200,242,.025);
      font: 8px/1.5 var(--mono);
    }
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
      .header { position: relative; align-items: flex-start; flex-direction: column; gap: 12px; }
      .header > div:first-child, .toolbar { width: 100%; }
      .header h1 { white-space: nowrap; }
      .tabs { top: 0; }
      .live-main-grid { grid-template-columns: 1fr; }
      .trajectory-header { align-items: flex-start; flex-direction: column; }
      .trajectory-actions {
        width: 100%; display: grid; grid-template-columns: minmax(0,1.25fr) minmax(0,1fr);
        gap: 6px; overflow: visible;
      }
      .trajectory-modes { grid-column: 1 / -1; }
      .trajectory-benchmark { width: 100%; }
      .trajectory-segmented { min-width: 0; width: 100%; }
      .trajectory-segmented button { min-width: 0; flex: 1 1 0; padding: 0 3px; font-size: 7px; }
      .trajectory-context { min-height: 50px; padding-top: 8px; padding-bottom: 8px; flex-wrap: wrap; white-space: normal; gap: 6px 12px; }
      .trajectory-stats { grid-template-columns: repeat(3, minmax(0,1fr)); gap: 13px 0; }
      .trajectory-stat:nth-child(4) { border-left: 0; padding-left: 0; }
      .trajectory-chart { height: 250px; }
      .trajectory-footer > span:last-child { display: none; }
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
      <div class="card tall trajectory-card">
        <div class="trajectory-header">
          <div class="trajectory-title-wrap">
            <span class="trajectory-glyph" aria-hidden="true"></span>
            <div class="trajectory-title">
              <h2>PORTFOLIO EQUITY CURVE</h2>
              <p id="trajectory-subtitle">读取日终权益账本…</p>
            </div>
          </div>
          <div class="trajectory-actions">
            <div class="trajectory-segmented trajectory-modes" aria-label="曲线指标" role="group">
              <button class="active" data-trajectory-mode="capital" onclick="setTrajectoryMode('capital')" aria-pressed="true">CAPITAL</button>
              <button data-trajectory-mode="return" onclick="setTrajectoryMode('return')" aria-pressed="false">RETURN</button>
              <button data-trajectory-mode="drawdown" onclick="setTrajectoryMode('drawdown')" aria-pressed="false">DRAWDOWN</button>
              <button data-trajectory-mode="exposure" onclick="setTrajectoryMode('exposure')" aria-pressed="false">EXPOSURE</button>
            </div>
            <select id="trajectory-benchmark" class="trajectory-benchmark" aria-label="对比基准" onchange="onTrajectoryBenchmarkChange()">
              <option value="000852.SH">CSI 1000</option>
              <option value="000300.SH">CSI 300</option>
            </select>
            <div class="trajectory-segmented" aria-label="曲线区间" role="group">
              <button data-trajectory-range="1m" onclick="setTrajectoryRange('1m')" aria-pressed="false">1M</button>
              <button data-trajectory-range="3m" onclick="setTrajectoryRange('3m')" aria-pressed="false">3M</button>
              <button data-trajectory-range="ytd" onclick="setTrajectoryRange('ytd')" aria-pressed="false">YTD</button>
              <button class="active" data-trajectory-range="all" onclick="setTrajectoryRange('all')" aria-pressed="true">ALL</button>
            </div>
          </div>
        </div>
        <div class="trajectory-context" id="trajectory-context">
          <span class="trajectory-feed observed"><i></i>EOD OBSERVED</span>
          <span class="trajectory-feed missing"><i></i>INTRADAY NOT INGESTED</span>
          <span class="trajectory-feed"><i></i>BENCHMARK LOADING</span>
        </div>
        <div class="trajectory-stats" id="trajectory-stats"></div>
        <div class="chart-container trajectory-chart"><canvas id="live-nav-chart"></canvas></div>
        <div class="trajectory-footer">
          <div class="trajectory-legend" id="trajectory-legend"><span><i></i>PORTFOLIO NAV</span><span><i class="hwm"></i>HIGH-WATER MARK</span></div>
          <span>HOVER TO INSPECT · RANGE IS LOCAL TO THIS CHART</span>
        </div>
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
        <h2>POSITION INVENTORY <span class="hint">EOD 市值 / NAV 权重</span></h2>
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
        <h2>STRATEGY → FILL PRICE ATTRIBUTION <span class="hint" id="execution-analysis-scope">当前实例</span></h2>
        <div class="execution-method">RAW STRATEGY REFERENCE → ACTUAL FILL VWAP · 方向调整后正值表示不利成交，负值表示价格改善</div>
        <div id="execution-price-table"><div class="loading">读取实例成交归因…</div></div>
      </div>
      <div class="card wide">
        <h2>Orders 状态矩阵 <span class="hint">当前实例 · 当前期间</span></h2>
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
    let LIVE_TRAJECTORY_DATA = null;
    let liveTrajectoryMode = 'capital';
    let liveTrajectoryRange = 'all';
    let liveBenchmarkSymbol = localStorage.getItem('qmt_dashboard_benchmark') || '000852.SH';

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
    function fmtSignedBps(x){
      if(x==null) return '—';
      const value=Number(x);
      return (value>0?'+':'')+value.toFixed(1)+' bp';
    }
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

    function renderPositionInventory(items, latestRisk = {}) {
      const el = document.getElementById('position-inventory');
      if (!items || !items.length) {
        el.innerHTML = '<div class="empty-state">暂无持仓，或实例尚未完成账本快照</div>';
        return;
      }
      const top = items.slice(0, 8);
      const maxWeight = Math.max(...top.map(i => Math.abs(Number(i.weight) || 0)), .0001);
      el.innerHTML = `<div class="metric-list">${top.map(i => `<div class="metric-line">
        <span class="metric-name">${esc(i.symbol)}</span><span class="metric-track"><i style="width:${Math.abs(Number(i.weight)||0)/maxWeight*100}%"></i></span>
        <span class="metric-value ${Number(i.weight)<0?'neg':'pos'}">${fmt(Number(i.weight),{pct:true,sign:true})}</span></div>`).join('')}</div>
        <div class="live-note">TOP ${top.length} BY |MARKET VALUE| · ${fmt(latestRisk.pricing_coverage,{pct:true})} PRICED<br>
          ${latestRisk.stale_mark_count||0} STALE · ${latestRisk.missing_mark_count||0} MISSING · EOD CLOSE / LAST CLOSE FALLBACK</div>`;
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

    const trajectoryCrosshairPlugin = {
      id: 'trajectoryCrosshair',
      afterDraw(chart) {
        const active = chart.tooltip?.getActiveElements?.() || [];
        if (!active.length) return;
        const x = active[0].element.x;
        const {ctx, chartArea} = chart;
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(x, chartArea.top);
        ctx.lineTo(x, chartArea.bottom);
        ctx.lineWidth = 1;
        ctx.strokeStyle = 'rgba(174,211,229,.28)';
        ctx.setLineDash([3, 4]);
        ctx.stroke();
        ctx.restore();
      },
    };

    function trajectoryDate(value) {
      const raw = String(value || '');
      if (/^\d{8}$/.test(raw)) return `${raw.slice(0,4)}-${raw.slice(4,6)}-${raw.slice(6,8)}`;
      return raw;
    }

    function trajectoryDateValue(value) {
      const raw = String(value || '').replaceAll('-', '');
      if (!/^\d{8}$/.test(raw)) return null;
      return new Date(Number(raw.slice(0,4)), Number(raw.slice(4,6))-1, Number(raw.slice(6,8)));
    }

    function trajectoryRows(riskData) {
      const ordered = (riskData?.items || []).slice().sort((a,b) => String(a.date).localeCompare(String(b.date)))
        .filter(i => Number.isFinite(Number(i.nav)));
      const latestDate = trajectoryDateValue(ordered.at(-1)?.date);
      const cutoff = latestDate ? new Date(latestDate) : null;
      if (cutoff && liveTrajectoryRange === '1m') cutoff.setDate(cutoff.getDate() - 31);
      if (cutoff && liveTrajectoryRange === '3m') cutoff.setDate(cutoff.getDate() - 93);
      if (cutoff && liveTrajectoryRange === 'ytd') cutoff.setMonth(0, 1);
      const filtered = liveTrajectoryRange === 'all' || !cutoff ? ordered : ordered.filter(i => {
        const date = trajectoryDateValue(i.date);
        return date && date >= cutoff;
      });
      let peak = -Infinity, performancePeak = 1, portfolioGrowth = 1;
      let alignedPortfolioGrowth = 1, alignedBenchmarkGrowth = 1, comparisonStarted = false;
      return filtered.map((item, index) => {
        const nav = Number(item.nav);
        peak = Math.max(peak, nav);
        const previous = index ? Number(filtered[index - 1].nav) : null;
        const portfolioReturn = item.portfolio_return == null ? null : Number(item.portfolio_return);
        const benchmarkReturn = item.benchmark_return == null ? null : Number(item.benchmark_return);
        if (index && portfolioReturn != null) portfolioGrowth *= 1 + portfolioReturn;
        performancePeak = Math.max(performancePeak, portfolioGrowth);
        const portfolioCumulativeReturn = portfolioGrowth - 1;
        let alignedPortfolioCumulativeReturn = null;
        let benchmarkCumulativeReturn = null;
        let excessCumulativeReturn = null;
        if (!comparisonStarted && item.benchmark_close != null) {
          comparisonStarted = true;
          alignedPortfolioCumulativeReturn = 0;
          benchmarkCumulativeReturn = 0;
          excessCumulativeReturn = 0;
        } else if (comparisonStarted && portfolioReturn != null && benchmarkReturn != null) {
          alignedPortfolioGrowth *= 1 + portfolioReturn;
          alignedBenchmarkGrowth *= 1 + benchmarkReturn;
          alignedPortfolioCumulativeReturn = alignedPortfolioGrowth - 1;
          benchmarkCumulativeReturn = alignedBenchmarkGrowth - 1;
          excessCumulativeReturn = alignedPortfolioGrowth - alignedBenchmarkGrowth;
        }
        return {
          date: item.date, nav, peak, portfolioReturn, benchmarkReturn,
          portfolioCumulativeReturn, alignedPortfolioCumulativeReturn,
          benchmarkCumulativeReturn, excessCumulativeReturn,
          externalCashFlow: Number(item.external_cash_flow) || 0,
          cashFlowStatus: item.cash_flow_status,
          grossExposure: item.gross_exposure == null ? null : Number(item.gross_exposure),
          netExposure: item.net_exposure == null ? null : Number(item.net_exposure),
          cashRatio: item.cash_ratio == null ? null : Number(item.cash_ratio),
          longMarketValue: Number(item.long_market_value) || 0,
          shortMarketValue: Number(item.short_market_value) || 0,
          pricingCoverage: item.pricing_coverage == null ? null : Number(item.pricing_coverage),
          staleMarkCount: Number(item.stale_mark_count) || 0,
          missingMarkCount: Number(item.missing_mark_count) || 0,
          dailyPnl: previous == null ? null : nav - previous - (Number(item.external_cash_flow) || 0),
          drawdown: performancePeak ? portfolioGrowth / performancePeak - 1 : 0,
        };
      });
    }

    function trajectoryComparison(rows) {
      const aligned = rows.slice(1).filter(row => row.portfolioReturn != null && row.benchmarkReturn != null);
      const p = aligned.map(row => row.portfolioReturn), b = aligned.map(row => row.benchmarkReturn);
      const mean = values => values.length ? values.reduce((a,c)=>a+c,0)/values.length : null;
      const sampleVar = values => {
        if (values.length < 2) return null;
        const avg = mean(values);
        return values.reduce((a,c)=>a+(c-avg)**2,0)/(values.length-1);
      };
      const pMean = mean(p), bMean = mean(b), bVar = sampleVar(b);
      const covariance = p.length < 2 ? null : p.reduce((a,c,i)=>a+(c-pMean)*(b[i]-bMean),0)/(p.length-1);
      const beta = bVar ? covariance / bVar : null;
      const pVar = sampleVar(p);
      const correlation = covariance == null || !pVar || !bVar ? null : covariance / Math.sqrt(pVar*bVar);
      const excess = p.map((value,index)=>value-b[index]);
      const excessVar = sampleVar(excess), trackingError = excessVar == null ? null : Math.sqrt(excessVar*252);
      const informationRatio = trackingError ? mean(excess)*252/trackingError : null;
      const last = rows.at(-1);
      const compound = values => values.reduce((growth,value)=>growth*(1+value),1)-1;
      return {
        alignedDays: aligned.length,
        rangePortfolioReturn: last?.portfolioCumulativeReturn ?? null,
        portfolioReturn: p.length ? compound(p) : null,
        benchmarkReturn: b.length ? compound(b) : null,
        excessReturn: p.length && b.length ? compound(p)-compound(b) : null,
        beta, correlation, trackingError, informationRatio,
      };
    }

    function setTrajectoryMode(mode) {
      liveTrajectoryMode = mode;
      document.querySelectorAll('[data-trajectory-mode]').forEach(button => {
        const active = button.dataset.trajectoryMode === mode;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
      });
      renderCapitalTrajectory(LIVE_TRAJECTORY_DATA);
    }

    function setTrajectoryRange(range) {
      liveTrajectoryRange = range;
      document.querySelectorAll('[data-trajectory-range]').forEach(button => {
        const active = button.dataset.trajectoryRange === range;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
      });
      renderCapitalTrajectory(LIVE_TRAJECTORY_DATA);
    }

    function onTrajectoryBenchmarkChange() {
      liveBenchmarkSymbol = document.getElementById('trajectory-benchmark').value;
      localStorage.setItem('qmt_dashboard_benchmark', liveBenchmarkSymbol);
      renderLive();
    }

    function trajectoryStat(label, value, cls = '') {
      return `<div class="trajectory-stat"><div class="trajectory-stat-label">${label}</div>
        <div class="trajectory-stat-value ${cls}">${value}</div></div>`;
    }

    function renderCapitalTrajectory(riskData) {
      LIVE_TRAJECTORY_DATA = riskData;
      const rows = trajectoryRows(riskData);
      const statsEl = document.getElementById('trajectory-stats');
      const subtitle = document.getElementById('trajectory-subtitle');
      if (!rows.length) {
        statsEl.innerHTML = '<div class="empty-state">当前实例尚无日终风险快照，请先运行回填</div>';
        subtitle.textContent = 'NO MATERIALIZED EOD RISK DATA';
        destroyChart('live-nav-chart');
        return;
      }

      const first = rows[0], last = rows.at(-1);
      const comparison = trajectoryComparison(rows);
      const benchmark = riskData?.benchmark || {};
      const benchmarkAvailable = Boolean(benchmark.available && comparison.alignedDays);
      if (!benchmarkAvailable) {
        comparison.benchmarkReturn = null;
        comparison.excessReturn = null;
      }
      const displayedPortfolioReturn = benchmarkAvailable
        ? comparison.portfolioReturn : comparison.rangePortfolioReturn;
      const pnl = rows.slice(1).reduce((sum,row)=>sum+(row.dailyPnl||0),0);
      const maxDrawdown = Math.min(...rows.map(row => row.drawdown));
      const totalFlow = rows.slice(1).reduce((sum,row)=>sum+row.externalCashFlow,0);
      subtitle.textContent = `EOD · ${rows.length} OBSERVATIONS · ${trajectoryDate(first.date)} → ${trajectoryDate(last.date)} · ${benchmark.name||benchmark.symbol||'NO BENCHMARK'}`;
      const flowClass = last.cashFlowStatus === 'observed' ? 'observed' : 'missing';
      const flowText = last.cashFlowStatus === 'observed'
        ? `CASH FLOW OBSERVED · ${fmt(totalFlow,{cur:true,sign:true})}`
        : (last.cashFlowStatus === 'missing_live' ? 'LIVE CASH FLOW JOURNAL MISSING'
          : (last.cashFlowStatus === 'not_applicable_shadow' ? 'SHADOW CASH LEDGER' : 'PAPER CASH FLOW ASSUMED ZERO'));
      document.getElementById('trajectory-context').innerHTML = `
        <span class="trajectory-feed observed"><i></i>EOD OBSERVED · ${rows.length} SNAPSHOTS</span>
        <span class="trajectory-feed ${benchmarkAvailable?'observed':'missing'}"><i></i>${benchmarkAvailable ? `${benchmark.name} · ${comparison.alignedDays} ALIGNED DAYS` : 'BENCHMARK UNAVAILABLE'}</span>
        <span class="trajectory-feed ${last.missingMarkCount?'missing':'observed'}"><i></i>PRICED ${fmt(last.pricingCoverage,{pct:true})} · ${last.staleMarkCount} STALE · ${last.missingMarkCount} MISSING</span>
        <span class="trajectory-feed ${flowClass}"><i></i>${flowText}</span>
        <span class="trajectory-feed missing"><i></i>INTRADAY NOT INGESTED</span>`;

      let stats;
      if (liveTrajectoryMode === 'return') {
        stats = [
          trajectoryStat('PORTFOLIO', fmt(displayedPortfolioReturn,{pct:true,sign:true}), colorOf(displayedPortfolioReturn)),
          trajectoryStat(benchmark.name||'BENCHMARK', fmt(comparison.benchmarkReturn,{pct:true,sign:true}), colorOf(comparison.benchmarkReturn)),
          trajectoryStat('EXCESS RETURN', fmt(comparison.excessReturn,{pct:true,sign:true}), colorOf(comparison.excessReturn)),
          trajectoryStat('BETA', fmt(comparison.beta,{dec:3})),
          trajectoryStat('TRACKING ERROR', fmt(comparison.trackingError,{pct:true})),
          trajectoryStat('INFORMATION RATIO', fmt(comparison.informationRatio,{dec:3}), colorOf(comparison.informationRatio)),
        ];
      } else if (liveTrajectoryMode === 'drawdown') {
        stats = [
          trajectoryStat('CURRENT DRAWDOWN', fmt(last.drawdown,{pct:true}), last.drawdown<0?'neg':''),
          trajectoryStat('MAX DRAWDOWN', fmt(maxDrawdown,{pct:true}), maxDrawdown<0?'neg':''),
          trajectoryStat('HIGH-WATER', fmt(last.peak,{curMM:true})),
          trajectoryStat('CURRENT NAV', fmt(last.nav,{curMM:true})),
          trajectoryStat('RANGE RETURN', fmt(comparison.rangePortfolioReturn,{pct:true,sign:true}), colorOf(comparison.rangePortfolioReturn)),
          trajectoryStat('OBSERVATIONS', String(rows.length)),
        ];
      } else if (liveTrajectoryMode === 'exposure') {
        stats = [
          trajectoryStat('GROSS EXPOSURE', fmt(last.grossExposure,{pct:true})),
          trajectoryStat('NET EXPOSURE', fmt(last.netExposure,{pct:true,sign:true}), colorOf(last.netExposure)),
          trajectoryStat('CASH / NAV', fmt(last.cashRatio,{pct:true,sign:true}), colorOf(last.cashRatio)),
          trajectoryStat('LONG MARKET VALUE', fmt(last.longMarketValue,{curMM:true})),
          trajectoryStat('SHORT MARKET VALUE', fmt(last.shortMarketValue,{curMM:true})),
          trajectoryStat('PRICING COVERAGE', fmt(last.pricingCoverage,{pct:true}), last.pricingCoverage<1?'warn':'pos'),
        ];
      } else {
        stats = [
          trajectoryStat('START NAV', fmt(first.nav,{curMM:true})),
          trajectoryStat('CURRENT NAV', fmt(last.nav,{curMM:true})),
          trajectoryStat('TRADING P&L', fmt(pnl,{cur:true,sign:true}), colorOf(pnl)),
          trajectoryStat('TOTAL RETURN', fmt(comparison.rangePortfolioReturn,{pct:true,sign:true}), colorOf(comparison.rangePortfolioReturn)),
          trajectoryStat('HIGH-WATER', fmt(last.peak,{curMM:true})),
          trajectoryStat('MAX DRAWDOWN', fmt(maxDrawdown,{pct:true}), maxDrawdown < 0 ? 'neg' : ''),
        ];
      }
      statsEl.innerHTML = stats.join('');

      const labels = rows.map(row => trajectoryDate(row.date));
      let tick = value => '¥' + (Number(value)/1e6).toFixed(2) + 'M';
      let legend = '<span><i></i>PORTFOLIO NAV</span><span><i class="hwm"></i>HIGH-WATER MARK</span>';
      let datasets = [{
        label: 'Portfolio NAV', data: rows.map(row=>row.nav), borderColor: '#51c8f2',
        backgroundColor: 'rgba(81,200,242,.10)', fill: true, tension: .22,
        pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: '#e7fbff',
        pointHoverBorderColor: '#51c8f2', pointHoverBorderWidth: 2, borderWidth: 2,
      }, {
        label: 'High-water mark', data: rows.map(row => row.peak), borderColor: 'rgba(147,168,191,.48)',
        backgroundColor: 'transparent', fill: false, tension: 0, pointRadius: 0,
        pointHoverRadius: 0, borderWidth: 1, borderDash: [4,4],
      }];
      if (liveTrajectoryMode === 'return') {
        tick = value => Number(value).toFixed(1) + '%';
        datasets = [{
          label:'Portfolio return', data:rows.map(row => (benchmarkAvailable
            ? (row.alignedPortfolioCumulativeReturn==null?null:row.alignedPortfolioCumulativeReturn*100)
            : row.portfolioCumulativeReturn*100)),
          borderColor:'#40d6a0', backgroundColor:'rgba(64,214,160,.07)', fill:true,
          tension:.2, pointRadius:0, pointHoverRadius:4, borderWidth:2,
        }];
        if (benchmarkAvailable) datasets.push({
          label:benchmark.name, data:rows.map(row=>row.benchmarkCumulativeReturn*100),
          borderColor:'#8fa5b8', backgroundColor:'transparent', fill:false,
          tension:.2, pointRadius:0, pointHoverRadius:3, borderWidth:1.3,
        }, {
          label:'Excess return', data:rows.map(row=>row.excessCumulativeReturn*100),
          borderColor:'#f4b860', backgroundColor:'transparent', fill:false,
          tension:.2, pointRadius:0, pointHoverRadius:3, borderWidth:1.5, borderDash:[5,3],
        });
        legend = `<span><i style="background:var(--positive)"></i>PORTFOLIO</span>${benchmarkAvailable?`<span><i style="background:#8fa5b8"></i>${esc(benchmark.name)}</span><span><i style="background:#f4b860"></i>EXCESS</span>`:''}`;
      } else if (liveTrajectoryMode === 'drawdown') {
        tick = value => Number(value).toFixed(1) + '%';
        datasets = [{
          label:'Drawdown', data:rows.map(row=>row.drawdown*100), borderColor:'#ff647c',
          backgroundColor:'rgba(255,100,124,.13)', fill:true, tension:.18,
          pointRadius:0, pointHoverRadius:4, borderWidth:2,
        }];
        legend = '<span><i style="background:var(--negative)"></i>UNDERWATER CURVE</span>';
      } else if (liveTrajectoryMode === 'exposure') {
        tick = value => Number(value).toFixed(0) + '%';
        datasets = [{
          label:'Gross exposure', data:rows.map(row=>row.grossExposure==null?null:row.grossExposure*100),
          borderColor:'#51c8f2', backgroundColor:'rgba(81,200,242,.06)', fill:true,
          tension:.18, pointRadius:0, pointHoverRadius:4, borderWidth:2,
        }, {
          label:'Net exposure', data:rows.map(row=>row.netExposure==null?null:row.netExposure*100),
          borderColor:'#40d6a0', backgroundColor:'transparent', fill:false,
          tension:.18, pointRadius:0, pointHoverRadius:3, borderWidth:1.5,
        }, {
          label:'Cash / NAV', data:rows.map(row=>row.cashRatio==null?null:row.cashRatio*100),
          borderColor:'#f4b860', backgroundColor:'transparent', fill:false,
          tension:.18, pointRadius:0, pointHoverRadius:3, borderWidth:1.3, borderDash:[5,3],
        }];
        legend = '<span><i></i>GROSS</span><span><i style="background:var(--positive)"></i>NET</span><span><i style="background:#f4b860"></i>CASH / NAV</span>';
      }
      document.getElementById('trajectory-legend').innerHTML = legend;

      destroyChart('live-nav-chart');
      charts['live-nav-chart'] = new Chart(document.getElementById('live-nav-chart'), {
        type: 'line', data: {labels, datasets}, plugins: [trajectoryCrosshairPlugin],
        options: {
          responsive: true, maintainAspectRatio: false, normalized: true,
          animation: {duration: 280}, interaction: {mode: 'index', intersect: false},
          layout: {padding: {left: 4, right: 4, top: 8, bottom: 0}},
          scales: {
            y: {
              position: 'right', beginAtZero: liveTrajectoryMode === 'drawdown',
              max: liveTrajectoryMode === 'drawdown' ? 0 : undefined,
              ticks: {color:'#65778a', font:{family:'SFMono-Regular',size:9}, callback:tick, maxTicksLimit:6},
              grid: {color:'rgba(114,139,164,.11)', drawTicks:false}, border:{display:false},
            },
            x: {
              ticks: {
                color:'#586a7e', font:{family:'SFMono-Regular',size:8},
                autoSkip:true, maxTicksLimit:window.innerWidth < 600 ? 5 : 7, maxRotation:0,
                callback:function(value){
                  const label = this.getLabelForValue(value);
                  return window.innerWidth < 600 ? label.slice(5) : label;
                },
              },
              grid: {display:false}, border:{color:'rgba(114,139,164,.16)'},
            },
          },
          plugins: {
            legend: {display:false},
            tooltip: {
              enabled:true, displayColors:false, padding:11, cornerRadius:7,
              backgroundColor:'rgba(5,10,15,.96)', borderColor:'rgba(81,200,242,.24)', borderWidth:1,
              titleColor:'#edf7ff', bodyColor:'#aebdcb', titleFont:{family:'SFMono-Regular',size:10},
              bodyFont:{family:'SFMono-Regular',size:9},
              callbacks: {
                title: items => items.length ? rows[items[0].dataIndex].date + ' · EOD' : '',
                label: context => {
                  const value = context.parsed.y;
                  if (context.dataset.label === 'Portfolio NAV' || context.dataset.label === 'High-water mark') {
                    return `${context.dataset.label}  ${fmt(value,{cur:true})}`;
                  }
                  return `${context.dataset.label}  ${Number(value).toFixed(2)}%`;
                },
                afterBody: items => {
                  if (!items.length) return [];
                  const row = rows[items[0].dataIndex];
                  return [
                    `Day P&L       ${fmt(row.dailyPnl,{cur:true,sign:true})}`,
                    `Portfolio     ${fmt(row.portfolioReturn,{pct:true,sign:true})}`,
                    `${String(benchmark.name||'Benchmark').padEnd(13,' ')}${fmt(row.benchmarkReturn,{pct:true,sign:true})}`,
                    `Cum. excess   ${fmt(row.excessCumulativeReturn,{pct:true,sign:true})}`,
                    `Drawdown      ${fmt(row.drawdown,{pct:true})}`,
                    `Gross / Net   ${fmt(row.grossExposure,{pct:true})} / ${fmt(row.netExposure,{pct:true})}`,
                    `Cash flow     ${fmt(row.externalCashFlow,{cur:true,sign:true})}`,
                  ];
                },
              },
            },
          },
        },
      });
    }

    async function renderLive() {
      try {
        const [snapshot, alerts, dailyRisk] = await Promise.all([
          api('/admin/ops/live-snapshot?' + selectedQuery({days:30})),
          api('/admin/alerts'),
          api('/admin/metrics/daily-risk?' + selectedQuery({period:'all', benchmark_symbol:liveBenchmarkSymbol})),
        ]);
        const inst = snapshot.instance || {}, risk = snapshot.risk || {}, execution = snapshot.execution || {};
        const latestRisk = dailyRisk.summary?.latest || {};
        const comparison = dailyRisk.summary || {};
        document.getElementById('live-asof').textContent = `AS OF ${snapshot.as_of || '—'}`;
        document.getElementById('live-kpis').innerHTML = `
          ${kpiCard('NAV · EOD', fmt(inst.nav,{cur:true}), '', `${inst.nav_date||'—'} · cash ${fmt(inst.cash_ratio,{pct:true})}`)}
          ${kpiCard('Day P&L · EOD', fmt(risk.daily_pnl,{cur:true}), colorOf(risk.daily_pnl), fmt(risk.daily_return,{pct:true,sign:true}))}
          ${kpiCard('Current Drawdown', fmt(risk.current_drawdown,{pct:true}), risk.current_drawdown<0?'neg':'pos', 'from high-water mark')}
          ${kpiCard('20D Ann. Vol', fmt(risk.rolling_volatility_20d,{pct:true}), risk.rolling_volatility_20d==null?'warn':'', `VaR ${fmt(risk.historical_var_95_1d,{pct:true})} · ES ${fmt(risk.expected_shortfall_95_1d,{pct:true})}`)}
          ${kpiCard('Gross Exposure · EOD', fmt(latestRisk.gross_exposure,{pct:true}), latestRisk.gross_exposure==null?'warn':'', `net ${fmt(latestRisk.net_exposure,{pct:true})} · cash ${fmt(latestRisk.cash_ratio,{pct:true})}`)}
          ${kpiCard(`Excess · ${dailyRisk.benchmark?.name||'Benchmark'}`, fmt(comparison.excess_return,{pct:true,sign:true}), colorOf(comparison.excess_return), `${dailyRisk.benchmark?.aligned_return_days||0} aligned days`)}
          ${kpiCard('Fill Rate · 30D', fmt(execution.fill_rate,{pct:true}), execution.fill_rate==null?'warn':(execution.fill_rate>=.9?'pos':(execution.fill_rate<.7?'neg':'warn')), `${execution.orders_total||0} orders`)}
          ${kpiCard('Exec Shortfall', fmtLiveBps(execution.weighted_shortfall_bps), execution.weighted_shortfall_bps==null?'warn':(Math.abs(execution.weighted_shortfall_bps)<=10?'pos':'warn'), 'directional · notional weighted')}`;
        document.getElementById('live-controls').innerHTML = liveControlRows(snapshot);
        renderCapitalTrajectory(dailyRisk);
        renderExecutionFunnel(execution);
        renderPositionInventory(dailyRisk.latest_positions, latestRisk);
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
          document.getElementById('execution-price-table').innerHTML =
            '<div class="empty-state">Shadow 实例没有真实订单或成交价格</div>';
          document.getElementById('execution-analysis-scope').textContent = getInstanceId();
          return;
        }
        const days = ({'7d':7, '30d':30, '90d':90, '180d':180, ytd:365, '1y':365, all:365}[period] || 30);
        const [execution, ordSummary] = await Promise.all([
          api('/admin/metrics/execution-analysis?' + selectedQuery({period, limit:200})),
          api('/admin/orders-summary?' + selectedQuery({days})),
        ]);
        const trade = execution.summary || {};

        const fillBadge = trade.fill_rate === null ? 'muted' :
                         (trade.fill_rate > 0.9 ? 'pos' : (trade.fill_rate > 0.7 ? 'warn' : 'neg'));
        const shortfallClass = trade.weighted_strategy_to_fill_bps == null ? 'warn' :
          (trade.weighted_strategy_to_fill_bps > 0 ? 'neg' : (trade.weighted_strategy_to_fill_bps < 0 ? 'pos' : ''));
        const costClass = trade.implementation_shortfall > 0 ? 'neg' :
          (trade.implementation_shortfall < 0 ? 'pos' : '');
        document.getElementById('kpis-trades').innerHTML = `
          ${kpiCard('订单总数', trade.n_orders, 'muted', `期间 ${period}`)}
          ${kpiCard('Fill Rate', fmt(trade.fill_rate, {pct:true}), fillBadge,
                   '有实际成交订单 / 总订单')}
          ${kpiCard('成交金额', fmt(trade.total_filled_amount, {curMM:true}), 'muted',
                   `${trade.filled_orders||0} 个成交订单`)}
          ${kpiCard('Strategy → Fill', fmtSignedBps(trade.weighted_strategy_to_fill_bps), shortfallClass,
                   '按实例成交金额加权 · 正值不利')}
          ${kpiCard('Implementation Cost', fmt(trade.implementation_shortfall,{cur:true}), costClass,
                   '相对策略参考价 · 正值为损耗')}
          ${kpiCard('Price Coverage', fmt(trade.strategy_price_coverage,{pct:true}), trade.strategy_price_coverage===1?'pos':'warn',
                   `arrival ${fmt(trade.arrival_price_coverage,{pct:true})}`)}
        `;
        document.getElementById('execution-analysis-scope').textContent =
          `${execution.instance_id} · ${period} · ${execution.count} FILLED`;

        const priceRows = execution.items || [];
        document.getElementById('execution-price-table').innerHTML = priceRows.length ? `
          <table><tr><th>Date / Fill Time</th><th>Symbol</th><th>Side</th>
            <th class="num">Strategy Px</th><th class="num">Limit Px</th>
            <th class="num">Actual Fill</th><th class="num">Δ Price</th>
            <th class="num">Strategy → Fill</th><th class="num">Alloc. Qty</th>
            <th class="num">Cost</th></tr>
          ${priceRows.map(item => {
            const adverseClass = item.strategy_to_fill_bps > 0 ? 'neg' :
              (item.strategy_to_fill_bps < 0 ? 'pos' : '');
            const fillQty = item.allocated_filled_quantity == null ? '—' :
              Number(item.allocated_filled_quantity).toLocaleString('en-US',{maximumFractionDigits:1});
            return `<tr><td>${esc(item.valid_date)}<br><span style="color:#617286;font-size:8px">${esc(item.filled_time||'—')}</span></td>
              <td>${esc(item.symbol)}</td><td class="${item.direction==='BUY'?'pos':'neg'}">${esc(item.direction)}</td>
              <td class="num">${fmt(item.strategy_reference_price,{dec:3})}</td>
              <td class="num">${fmt(item.limit_price,{dec:3})}</td>
              <td class="num">${fmt(item.fill_vwap,{dec:3})}</td>
              <td class="num ${adverseClass}">${fmt(item.raw_price_difference,{dec:3,sign:true})}</td>
              <td class="num ${adverseClass}">${fmtSignedBps(item.strategy_to_fill_bps)}</td>
              <td class="num">${fillQty}</td>
              <td class="num ${adverseClass}">${fmt(item.implementation_shortfall,{cur:true})}</td></tr>`;
          }).join('')}</table>
          <div class="live-note">STRATEGY PX = raw_signals.reference_price · ACTUAL FILL = execution_quality.fill_vwap，历史数据回退 trades.filled_price<br>
            BUY 成交价高于策略价、SELL 成交价低于策略价均记为正 bp / 正成本（不利成交）。</div>`
          : '<div class="empty-state">当前实例和期间没有可归因的实际成交</div>';

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
        document.getElementById('execution-price-table').innerHTML = `<div class="error">${esc(e.message)}</div>`;
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
      if (!['000852.SH','000300.SH'].includes(liveBenchmarkSymbol)) liveBenchmarkSymbol = '000852.SH';
      document.getElementById('trajectory-benchmark').value = liveBenchmarkSymbol;
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
