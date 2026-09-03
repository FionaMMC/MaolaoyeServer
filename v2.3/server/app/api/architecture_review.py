"""Collaborative server architecture/risk review subpage and note APIs."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.auth import verify_api_key
from app.dependencies import get_architecture_review_service
from app.exceptions import APIError, ErrorCode
from app.review_catalog import REVIEW_CATALOG
from app.schemas.architecture_review import (
    ArchitectureReviewCommentCreate,
    ArchitectureReviewCommentItem,
    ArchitectureReviewDecisionItem,
    ArchitectureReviewDecisionUpsert,
    ArchitectureReviewSessionData,
)
from app.schemas.common import APIResponse
from app.services.architecture_review import ArchitectureReviewService


router = APIRouter()


_REVIEW_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Server Architecture & Risk Review</title>
  <style>
    :root {
      --bg: #090e13; --panel: #101820; --panel-2: #141f29; --line: #273642;
      --line-soft: rgba(130,154,174,.16); --text: #dbe5ed; --muted: #8495a3;
      --faint: #566775; --cyan: #57d4d0; --blue: #76a9ff; --amber: #f1bd66;
      --red: #ff7878; --green: #72d39b; --violet: #ba9cff;
      --sans: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
      --mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body { margin: 0; color: var(--text); background:
      radial-gradient(circle at 78% -10%, rgba(70,120,145,.16), transparent 34%),
      radial-gradient(circle at 5% 35%, rgba(50,105,98,.09), transparent 29%), var(--bg);
      font: 14px/1.55 var(--sans); min-height: 100vh; }
    button, input, select, textarea { font: inherit; }
    button, a { -webkit-tap-highlight-color: transparent; }
    a { color: inherit; }
    .shell { max-width: 1540px; margin: 0 auto; padding: 18px 22px 60px; }
    .topbar { position: sticky; top: 0; z-index: 20; margin: -18px -22px 0;
      padding: 14px 22px 11px; background: rgba(9,14,19,.92); backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--line-soft); }
    .header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }
    .eyebrow { color: var(--cyan); font: 10px/1.2 var(--mono); letter-spacing: .16em; text-transform: uppercase; }
    h1 { margin: 6px 0 3px; color: #f2f7fa; font-size: clamp(20px, 2.3vw, 31px); line-height: 1.14; letter-spacing: -.025em; }
    .subtitle { color: var(--muted); font-size: 12px; max-width: 780px; }
    .header-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px; align-items: center; }
    .control, .btn { border: 1px solid var(--line); border-radius: 7px; background: #0c1319; color: var(--text); min-height: 35px; padding: 7px 10px; }
    .control { width: 135px; }
    .btn { cursor: pointer; font-size: 12px; text-decoration: none; display: inline-flex; align-items: center; justify-content: center; gap: 6px; }
    .btn:hover { border-color: #496274; background: #14202a; }
    .btn.primary { background: var(--cyan); border-color: var(--cyan); color: #071012; font-weight: 750; }
    .btn.danger { color: var(--red); }
    .sync { margin-top: 9px; display: flex; align-items: center; justify-content: space-between; gap: 12px; color: var(--faint); font: 10px var(--mono); }
    .sync .dot { width: 6px; height: 6px; display: inline-block; border-radius: 50%; background: var(--faint); margin-right: 6px; }
    .sync .dot.ok { background: var(--green); box-shadow: 0 0 10px rgba(114,211,155,.6); }
    .sync .dot.bad { background: var(--red); }
    .tabs { display: flex; gap: 4px; margin: 16px 0 13px; padding: 4px; overflow-x: auto;
      border: 1px solid var(--line-soft); border-radius: 9px; background: rgba(16,24,32,.76); }
    .tab { flex: 0 0 auto; border: 0; border-radius: 6px; background: transparent; color: var(--muted); padding: 8px 13px; cursor: pointer; font-size: 12px; }
    .tab:hover { color: var(--text); background: #17232d; }
    .tab.active { color: #081113; background: var(--cyan); font-weight: 750; }
    .view { display: none; }
    .view.active { display: block; }
    .hero { padding: 22px; border: 1px solid var(--line); border-radius: 12px; background: linear-gradient(120deg, rgba(20,32,41,.96), rgba(12,20,26,.96)); }
    .hero-grid { display: grid; grid-template-columns: minmax(0,1.5fr) minmax(260px,.6fr); gap: 26px; }
    h2 { margin: 0; font-size: 18px; color: #eef5f8; }
    h3 { margin: 0; font-size: 14px; color: #e5edf2; }
    .hero p { margin: 8px 0 0; color: var(--muted); max-width: 830px; }
    .asof { font: 10px/1.6 var(--mono); color: var(--faint); text-align: right; }
    .kpis { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 9px; margin-top: 13px; }
    .kpi { padding: 13px 14px; min-height: 83px; background: var(--panel); border: 1px solid var(--line-soft); border-radius: 9px; }
    .kpi b { display: block; color: #f2f8fb; font: 23px/1.15 var(--mono); }
    .kpi span { display: block; margin-top: 7px; color: var(--muted); font-size: 11px; }
    .current-grid { display: grid; grid-template-columns: repeat(5,minmax(0,1fr)); gap: 8px; }
    .current-card { padding: 12px; border: 1px solid var(--line-soft); border-radius: 9px; background: rgba(10,17,23,.78); }
    .current-card small { color: var(--faint); font: 9px var(--mono); text-transform: uppercase; }
    .current-card b { display: block; margin: 6px 0; color: var(--cyan); font: 14px var(--mono); }
    .current-card span { color: var(--muted); font-size: 10px; }
    .principles { display: grid; gap: 7px; margin-top: 14px; }
    .principle { display: grid; grid-template-columns: 22px minmax(0,1fr); gap: 9px; padding: 10px 12px; border: 1px solid var(--line-soft); background: rgba(8,14,19,.45); border-radius: 8px; color: #bdcbd5; }
    .principle i { color: var(--cyan); font: 11px var(--mono); font-style: normal; }
    .section-head { display: flex; justify-content: space-between; gap: 18px; align-items: flex-end; margin: 21px 0 10px; }
    .section-head p { margin: 4px 0 0; color: var(--muted); font-size: 11px; }
    .section-code { color: var(--faint); font: 10px var(--mono); }
    .flow-list { display: grid; gap: 12px; }
    .flow-card, .boundary-card, .question, .panel { border: 1px solid var(--line); background: rgba(16,24,32,.88); border-radius: 10px; }
    .flow-head { padding: 15px 17px; display: flex; justify-content: space-between; gap: 16px; border-bottom: 1px solid var(--line-soft); }
    .flow-head p { margin: 4px 0 0; color: var(--muted); font-size: 11px; }
    .review-trigger { border: 1px solid var(--line); color: var(--muted); background: #0b1218; border-radius: 6px; cursor: pointer; padding: 5px 8px; white-space: nowrap; font-size: 10px; }
    .review-trigger:hover { color: var(--cyan); border-color: var(--cyan); }
    .steps { display: grid; }
    .step { display: grid; grid-template-columns: 46px 120px minmax(210px,.9fr) minmax(260px,1.25fr); gap: 13px; padding: 12px 17px; border-top: 1px solid rgba(130,154,174,.09); align-items: start; }
    .step:first-child { border-top: 0; }
    .step-code { color: var(--cyan); font: 10px var(--mono); }
    .step strong { font-size: 12px; color: #dbe5ec; }
    .step-path { color: #b7c5cf; font: 11px/1.55 var(--mono); }
    .step-note { color: var(--muted); font-size: 11px; }
    .boundary-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 10px; }
    .boundary-card { padding: 15px; }
    .boundary-card .layer { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
    .boundary-card dl { margin: 12px 0 0; display: grid; gap: 9px; }
    .boundary-card dt { color: var(--faint); font: 9px var(--mono); text-transform: uppercase; }
    .boundary-card dd { margin: 2px 0 0; color: #b9c6cf; font-size: 11px; }
    .boundary-card .current { color: var(--amber); }
    .inventory-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 9px; }
    .inventory-card { padding: 13px 15px; border: 1px solid var(--line); background: rgba(16,24,32,.88); border-radius: 9px; }
    .inventory-card h3 { color: var(--cyan); font: 12px var(--mono); }
    .inventory-card p { margin: 7px 0 0; color: #b9c6cf; font-size: 11px; overflow-wrap: anywhere; }
    .inventory-card .inventory-meta { color: var(--faint); font: 9px/1.55 var(--mono); }
    .inventory-card .inventory-note { color: var(--amber); }
    .toolbar { display: grid; grid-template-columns: minmax(220px,1.8fr) repeat(4,minmax(125px,.6fr)); gap: 7px; padding: 11px; border: 1px solid var(--line); border-radius: 9px; background: var(--panel); position: sticky; top: 116px; z-index: 10; }
    .toolbar input, .toolbar select { width: 100%; border: 1px solid var(--line); background: #090f14; color: var(--text); border-radius: 6px; padding: 8px 9px; min-height: 35px; }
    .risk-summary { display: flex; flex-wrap: wrap; gap: 6px; margin: 9px 0; }
    .chip, .badge { display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 999px; padding: 3px 7px; color: var(--muted); background: #0b1218; font: 9px var(--mono); white-space: nowrap; }
    .badge.hard { color: var(--red); border-color: rgba(255,120,120,.32); background: rgba(255,120,120,.07); }
    .badge.isolation { color: var(--violet); border-color: rgba(186,156,255,.32); background: rgba(186,156,255,.07); }
    .badge.monitor { color: var(--blue); border-color: rgba(118,169,255,.32); background: rgba(118,169,255,.07); }
    .badge.gap { color: var(--amber); border-color: rgba(241,189,102,.35); background: rgba(241,189,102,.07); }
    .likelihood.observed { color: var(--red); }.likelihood.possible { color: var(--amber); }
    .likelihood.conditional { color: var(--blue); }.likelihood.low { color: var(--green); }.likelihood.unknown { color: var(--violet); }
    .table-wrap { border: 1px solid var(--line); border-radius: 10px; overflow: auto; background: rgba(14,22,29,.9); }
    table { width: 100%; border-collapse: collapse; min-width: 1250px; table-layout: fixed; }
    th { position: sticky; top: 0; z-index: 2; padding: 9px 10px; text-align: left; color: var(--faint); background: #111b23; border-bottom: 1px solid var(--line); font: 9px var(--mono); text-transform: uppercase; }
    td { vertical-align: top; padding: 11px 10px; border-top: 1px solid rgba(130,154,174,.1); color: #aebdc8; font-size: 11px; overflow-wrap: anywhere; }
    tbody tr:first-child td { border-top: 0; }
    tbody tr:hover td { background: rgba(35,51,62,.3); }
    .risk-id { color: var(--cyan); font: 9px var(--mono); margin-bottom: 4px; }
    .risk-name { color: #e4edf3; font-weight: 650; }
    .risk-now { color: #d0bd8e; }
    .source { margin-top: 6px; color: #617481; font: 9px/1.45 var(--mono); }
    .decision-cell { display: grid; gap: 6px; }
    .decision-line { font: 9px var(--mono); color: var(--muted); }
    .status-confirmed { color: var(--green); }.status-change_required { color: var(--red); }
    .status-follow_up { color: var(--amber); }.status-not_applicable { color: var(--faint); }
    .question-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 10px; }
    .question { padding: 16px; display: grid; gap: 9px; }
    .question p { margin: 0; color: #c2d0da; }
    .question .why { color: var(--muted); font-size: 11px; }
    .minutes { display: grid; grid-template-columns: minmax(0,1fr) minmax(300px,.42fr); gap: 12px; }
    .panel { padding: 16px; }
    .minute-group { margin-top: 14px; }
    .minute-item { padding: 10px 0; border-top: 1px solid var(--line-soft); }
    .minute-item:first-child { border-top: 0; }
    .minute-item strong { color: #dfe9ef; font-size: 12px; }
    .minute-item p { margin: 5px 0 0; color: var(--muted); font-size: 11px; white-space: pre-wrap; }
    .empty { padding: 28px; text-align: center; color: var(--faint); }
    .overlay { position: fixed; inset: 0; z-index: 80; background: rgba(0,0,0,.62); display: none; }
    .overlay.open { display: block; }
    .drawer { position: absolute; right: 0; top: 0; bottom: 0; width: min(560px, 100%); overflow-y: auto; background: #0d151c; border-left: 1px solid var(--line); box-shadow: -30px 0 90px rgba(0,0,0,.45); padding: 20px; }
    .drawer-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
    .drawer-title { margin: 4px 0 0; font-size: 18px; }
    .close { width: 34px; height: 34px; border: 1px solid var(--line); border-radius: 50%; background: transparent; color: var(--muted); cursor: pointer; }
    .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; margin-top: 18px; }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 10px; }
    label.full { grid-column: 1 / -1; }
    textarea, .drawer input, .drawer select { width: 100%; border: 1px solid var(--line); border-radius: 7px; background: #080e13; color: var(--text); padding: 9px; }
    textarea { min-height: 92px; resize: vertical; }
    .drawer-actions { display: flex; gap: 7px; justify-content: flex-end; margin-top: 9px; }
    .comments { display: grid; gap: 8px; margin-top: 12px; }
    .comment { border: 1px solid var(--line-soft); background: #101a22; border-radius: 8px; padding: 10px; }
    .comment-meta { display: flex; justify-content: space-between; gap: 8px; color: var(--faint); font: 9px var(--mono); }
    .comment-body { margin-top: 6px; color: #c5d2db; white-space: pre-wrap; overflow-wrap: anywhere; }
    .toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); z-index: 120; display: none; padding: 9px 13px; border: 1px solid var(--line); border-radius: 8px; background: #16222b; box-shadow: 0 15px 50px rgba(0,0,0,.45); color: var(--text); font-size: 11px; }
    @media (max-width: 1050px) {
      .boundary-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }
      .toolbar { grid-template-columns: repeat(2,minmax(0,1fr)); top: 148px; }
      .toolbar input { grid-column: 1/-1; }
      .kpis { grid-template-columns: repeat(2,minmax(0,1fr)); }
      .current-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }
      .step { grid-template-columns: 42px 100px minmax(0,1fr); }
      .step-note { grid-column: 3; }
    }
    @media (max-width: 720px) {
      .shell { padding: 12px 12px 45px; }.topbar { margin: -12px -12px 0; padding: 11px 12px 8px; }
      .header { display: block; }.header-actions { margin-top: 11px; justify-content: flex-start; }
      .control { flex: 1; min-width: 110px; }.tabs { margin-top: 11px; }
      .hero { padding: 16px; }.hero-grid, .minutes { grid-template-columns: 1fr; }.asof { text-align: left; margin-top: 10px; }
      .boundary-grid, .question-grid, .inventory-grid, .current-grid { grid-template-columns: 1fr; }
      .step { grid-template-columns: 38px minmax(0,1fr); gap: 7px 9px; }.step-path,.step-note { grid-column: 2; }
      .toolbar { position: relative; top: auto; grid-template-columns: 1fr; }.toolbar input { grid-column: auto; }
      .table-wrap { border: 0; overflow: visible; background: transparent; }
      table, thead, tbody, th, td, tr { display: block; min-width: 0; width: auto; }
      thead { display: none; } tbody { display: grid; gap: 9px; }
      tbody tr { border: 1px solid var(--line); border-radius: 9px; background: var(--panel); padding: 12px; }
      td { padding: 7px 0; border: 0; } td:before { content: attr(data-label); display: block; color: var(--faint); font: 8px var(--mono); margin-bottom: 3px; text-transform: uppercase; }
      .form-grid { grid-template-columns: 1fr; } label.full { grid-column: auto; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="topbar">
      <div class="header">
        <div>
          <div class="eyebrow">AURORA QUANT / DESIGN REVIEW</div>
          <h1>Server 业务流程与风控闸门审阅</h1>
          <div class="subtitle">把不相关的事情拆开，把每个风控在防什么、现在是否可能发生、触发后怎么办逐项说清。</div>
        </div>
        <div class="header-actions">
          <input class="control" id="reviewer" placeholder="你的名字" aria-label="你的名字">
          <input class="control" id="api-key" type="password" placeholder="API Key" aria-label="API Key">
          <button class="btn primary" onclick="connectNotes()">连接批注</button>
          <button class="btn" onclick="exportMarkdown()">导出纪要</button>
          <a class="btn" href="/dashboard">返回 Dashboard</a>
        </div>
      </div>
      <div class="sync"><span><i id="sync-dot" class="dot"></i><span id="sync-text">材料可离线阅读；连接 API Key 后启用共享批注</span></span><span id="session-rev">—</span></div>
    </div>

    <nav class="tabs" aria-label="审阅章节">
      <button class="tab active" data-view="overview" onclick="showView('overview')">00 会前总览</button>
      <button class="tab" data-view="flows" onclick="showView('flows')">01 业务流程</button>
      <button class="tab" data-view="boundaries" onclick="showView('boundaries')">02 设计边界</button>
      <button class="tab" data-view="risks" onclick="showView('risks')">03 风控闸门</button>
      <button class="tab" data-view="questions" onclick="showView('questions')">04 待讨论</button>
      <button class="tab" data-view="minutes" onclick="showView('minutes')">05 会议纪要</button>
    </nav>

    <main>
      <section class="view active" id="view-overview"></section>
      <section class="view" id="view-flows"></section>
      <section class="view" id="view-boundaries"></section>
      <section class="view" id="view-risks"></section>
      <section class="view" id="view-questions"></section>
      <section class="view" id="view-minutes"></section>
    </main>
  </div>

  <div class="overlay" id="overlay" onclick="if(event.target===this) closeDrawer()">
    <aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
      <div class="drawer-head"><div><div class="eyebrow" id="drawer-id">—</div><h2 class="drawer-title" id="drawer-title">审阅项</h2></div><button class="close" onclick="closeDrawer()" aria-label="关闭">×</button></div>
      <div class="form-grid">
        <label>共同结论
          <select id="decision-status" onchange="drawerDirty=true">
            <option value="pending">待讨论</option><option value="confirmed">已确认，无需修改</option>
            <option value="change_required">确认要修改</option><option value="follow_up">需补证 / 跟进</option>
            <option value="not_applicable">现阶段不适用</option>
          </select>
        </label>
        <label>负责人<input id="decision-owner" maxlength="80" placeholder="姓名 / 角色" oninput="drawerDirty=true"></label>
        <label class="full">结论与修改口径<textarea id="decision-rationale" maxlength="4000" placeholder="记录决定、修改边界、验收条件……" oninput="drawerDirty=true"></textarea></label>
      </div>
      <div class="drawer-actions"><button class="btn primary" onclick="saveDecision()">保存共同结论</button></div>
      <div class="section-head"><div><h3>讨论记录</h3><p>评论追加保存，适合两个人同时记。</p></div></div>
      <div id="drawer-comments" class="comments"></div>
      <label style="margin-top:12px">新增记录<textarea id="comment-body" maxlength="4000" placeholder="问题、反例、证据、待办……"></textarea></label>
      <div class="drawer-actions"><button class="btn" onclick="addComment()">追加记录</button></div>
    </aside>
  </div>
  <div class="toast" id="toast"></div>

  <script>
    const CATALOG = __REVIEW_CATALOG__;
    const API_BASE = window.location.origin;
    const TYPE_LABEL = {hard:'硬阻断', isolation:'降级/隔离', monitor:'检测/告警', gap:'当前缺口'};
    const LIKELIHOOD_LABEL = {observed:'已发生过', possible:'现在可能', conditional:'条件成立时可能', low:'当前较低', unknown:'需运行证据'};
    const STATUS_LABEL = {pending:'待讨论', confirmed:'已确认', change_required:'要修改', follow_up:'需补证', not_applicable:'不适用'};
    let API_KEY = localStorage.getItem('qmt_api_key') || '';
    let REVIEWER = localStorage.getItem('qmt_review_reviewer') || '';
    let shared = {comments:[], decisions:[], updated_at:null};
    let activeItemId = null;
    let drawerDirty = false;

    const allItems = [
      ...CATALOG.flows.map(x => ({id:x.id, title:x.name, kind:'流程'})),
      ...CATALOG.boundaries.map(x => ({id:x.id, title:x.layer, kind:'边界'})),
      ...CATALOG.risks.map(x => ({id:x.id, title:x.name, kind:'风控'})),
      ...CATALOG.questions.map(x => ({id:x.id, title:x.title, kind:'问题'})),
    ];
    const itemMap = Object.fromEntries(allItems.map(x => [x.id,x]));

    function esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
    function decisionFor(id) { return shared.decisions.find(x => x.item_id === id) || {status:'pending',rationale:'',owner:'',updated_by:'',updated_at:''}; }
    function commentsFor(id) { return shared.comments.filter(x => x.item_id === id); }
    function statusHtml(id) { const d=decisionFor(id), n=commentsFor(id).length; return `<span class="decision-line status-${esc(d.status)}">${esc(STATUS_LABEL[d.status]||d.status)} · ${n} 条记录</span>`; }
    function reviewButton(id) { return `<button class="review-trigger" onclick="openReview('${id}')">${statusHtml(id)} ↗</button>`; }
    function toast(message) { const el=document.getElementById('toast'); el.textContent=message; el.style.display='block'; clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.style.display='none',2200); }
    function reviewerRequired() { const value=document.getElementById('reviewer').value.trim(); if(!value){ toast('先填写你的名字'); document.getElementById('reviewer').focus(); return null; } REVIEWER=value; localStorage.setItem('qmt_review_reviewer',value); return value; }

    function renderOverview() {
      const gaps=CATALOG.risks.filter(x=>x.type==='gap').length;
      const possible=CATALOG.risks.filter(x=>['observed','possible'].includes(x.likelihood)).length;
      document.getElementById('view-overview').innerHTML = `
        <div class="hero"><div class="hero-grid"><div><div class="eyebrow">MEETING OBJECTIVE</div><h2>${esc(CATALOG.meta.subtitle)}</h2><p>${esc(CATALOG.meta.basis)}</p></div><div class="asof">CATALOG AS-OF ${esc(CATALOG.meta.as_of)}<br>SESSION ${esc(shared.session_id||'not connected')}<br>注：本页“现阶段判断”不是生产健康证明</div></div>
        <div class="principles">${CATALOG.principles.map((x,i)=>`<div class="principle"><i>0${i+1}</i><span>${esc(x)}</span></div>`).join('')}</div></div>
        <div class="kpis"><div class="kpi"><b>${CATALOG.flows.length}</b><span>条独立业务链</span></div><div class="kpi"><b>${CATALOG.risks.length}</b><span>个风控/缺口审阅项</span></div><div class="kpi"><b>${gaps}</b><span>个当前缺口</span></div><div class="kpi"><b>${possible}</b><span>已发生或现在可能</span></div></div>
        <div class="section-head"><div><h2>当前仓库快照</h2><p>用于限定“现阶段”的讨论口径；生产事实仍需现场证据。</p></div><span class="section-code">LOCAL EVIDENCE</span></div>
        <div class="current-grid">${CATALOG.current_state.map(x=>`<div class="current-card"><small>${esc(x.label)}</small><b>${esc(x.value)}</b><span>${esc(x.detail)}</span></div>`).join('')}</div>
        <div class="section-head"><div><h2>建议会议顺序</h2><p>先对齐边界，再逐条讨论“现在能不能发生”，最后只把有结论的项写入纪要。</p></div></div>
        <div class="flow-card"><div class="steps">
          <div class="step"><span class="step-code">01</span><strong>确认四条链</strong><span class="step-path">常规订单 / Hydra 专用 / 纯影子 / 监控</span><span class="step-note">不同链路不混用“已完成”“已对账”“已风控”等状态词。</span></div>
          <div class="step"><span class="step-code">02</span><strong>优先看缺口</strong><span class="step-path">风控矩阵 → 类型=当前缺口</span><span class="step-note">先讨论 P0，再看已发生过和现在可能的项。</span></div>
          <div class="step"><span class="step-code">03</span><strong>逐项落结论</strong><span class="step-path">点“审阅” → 共同结论 / 负责人 / 记录</span><span class="step-note">两人页面每 10 秒同步；评论追加，结论为共享最新值。</span></div>
          <div class="step"><span class="step-code">04</span><strong>导出纪要</strong><span class="step-path">会议纪要 → 导出 Markdown</span><span class="step-note">只导出已记录内容，并保留每项代码来源。</span></div>
        </div></div>`;
    }

    function renderFlows() {
      document.getElementById('view-flows').innerHTML = `<div class="section-head"><div><h2>端到端业务流程</h2><p>每条泳道有自己的输入、状态与终态；箭头相邻不代表应该耦合。</p></div><span class="section-code">${CATALOG.flows.length} LANES</span></div><div class="flow-list">${CATALOG.flows.map(f=>`
        <article class="flow-card"><div class="flow-head"><div><h3>${esc(f.name)}</h3><p>${esc(f.scope)}</p></div>${reviewButton(f.id)}</div><div class="steps">${f.steps.map(s=>`<div class="step"><span class="step-code">${esc(s[0])}</span><strong>${esc(s[1])}</strong><span class="step-path">${esc(s[2])}</span><span class="step-note">${esc(s[3])}</span></div>`).join('')}</div></article>`).join('')}</div>
        <div class="section-head"><div><h2>接口面索引</h2><p>按业务能力分组；相邻接口不等于共享事务。</p></div><span class="section-code">${CATALOG.interfaces.length} GROUPS</span></div>
        <div class="inventory-grid">${CATALOG.interfaces.map(x=>`<article class="inventory-card"><h3>${esc(x.group)}</h3><p>${esc(x.paths)}</p><p class="inventory-meta">OWNER · ${esc(x.owner)}</p><p class="inventory-note">${esc(x.note)}</p></article>`).join('')}</div>
        <div class="section-head"><div><h2>事实存储与写入边界</h2><p>每类事实只有指定 writer；尤其不要让监控或影子链反向写交易账。</p></div><span class="section-code">${CATALOG.stores.length} STORES</span></div>
        <div class="inventory-grid">${CATALOG.stores.map(x=>`<article class="inventory-card"><h3>${esc(x.name)}</h3><p>${esc(x.facts)}</p><p class="inventory-meta">WRITE · ${esc(x.writers)}<br>READ · ${esc(x.consumers)}</p><p class="inventory-note">${esc(x.boundary)}</p></article>`).join('')}</div>`;
    }

    function renderBoundaries() {
      document.getElementById('view-boundaries').innerHTML = `<div class="section-head"><div><h2>模块职责与禁止越界</h2><p>审阅重点不是文件放哪，而是谁拥有最终决定权、谁绝不能写哪类状态。</p></div></div><div class="boundary-grid">${CATALOG.boundaries.map(b=>`
        <article class="boundary-card"><div class="layer"><h3>${esc(b.layer)}</h3>${reviewButton(b.id)}</div><dl><div><dt>负责</dt><dd>${esc(b.owns)}</dd></div><div><dt>不应负责</dt><dd>${esc(b.must_not)}</dd></div><div><dt>当前判断</dt><dd class="current">${esc(b.current)}</dd></div><div><dt>代码位置</dt><dd class="source">${esc(b.source)}</dd></div></dl></article>`).join('')}</div>`;
    }

    function riskFilters() {
      const q=document.getElementById('risk-search')?.value.trim().toLowerCase()||'';
      const phase=document.getElementById('risk-phase')?.value||'';
      const type=document.getElementById('risk-type')?.value||'';
      const like=document.getElementById('risk-likelihood')?.value||'';
      const status=document.getElementById('risk-status')?.value||'';
      return CATALOG.risks.filter(r => (!phase||r.phase===phase)&&(!type||r.type===type)&&(!like||r.likelihood===like)&&(!status||decisionFor(r.id).status===status)&&(!q||JSON.stringify(r).toLowerCase().includes(q)));
    }
    function filterOptions(values, labels={}) { return [...new Set(values)].map(x=>`<option value="${esc(x)}">${esc(labels[x]||x)}</option>`).join(''); }
    function renderRisks(reset=false) {
      const root=document.getElementById('view-risks');
      if(reset || !document.getElementById('risk-search')) {
        root.innerHTML=`<div class="section-head"><div><h2>风控闸门与当前发生可能性</h2><p>“硬阻断”才会停止业务；“检测/告警”只告诉你出事了；“当前缺口”表示尚未防住。</p></div><span class="section-code">CODE-GROUNDED</span></div>
          <div class="toolbar"><input id="risk-search" placeholder="搜索风险、机制、代码位置…" oninput="updateRiskRows()"><select id="risk-phase" onchange="updateRiskRows()"><option value="">全部环节</option>${filterOptions(CATALOG.risks.map(x=>x.phase))}</select><select id="risk-type" onchange="updateRiskRows()"><option value="">全部类型</option>${filterOptions(CATALOG.risks.map(x=>x.type),TYPE_LABEL)}</select><select id="risk-likelihood" onchange="updateRiskRows()"><option value="">全部可能性</option>${filterOptions(CATALOG.risks.map(x=>x.likelihood),LIKELIHOOD_LABEL)}</select><select id="risk-status" onchange="updateRiskRows()"><option value="">全部会议状态</option>${filterOptions(Object.keys(STATUS_LABEL),STATUS_LABEL)}</select></div>
          <div id="risk-summary" class="risk-summary"></div><div class="table-wrap"><table><colgroup><col style="width:8%"><col style="width:13%"><col style="width:16%"><col style="width:18%"><col style="width:18%"><col style="width:16%"><col style="width:11%"></colgroup><thead><tr><th>环节 / 编号</th><th>风控</th><th>在防什么</th><th>机制 / 触发行为</th><th>现阶段会不会发生</th><th>残余风险</th><th>会议结论</th></tr></thead><tbody id="risk-body"></tbody></table></div>`;
      }
      updateRiskRows();
    }
    function updateRiskRows() {
      const rows=riskFilters(); const gaps=rows.filter(x=>x.type==='gap').length; const p0=rows.filter(x=>x.priority==='P0').length;
      document.getElementById('risk-summary').innerHTML=`<span class="chip">显示 ${rows.length}/${CATALOG.risks.length}</span><span class="chip">P0 ${p0}</span><span class="chip">当前缺口 ${gaps}</span>`;
      document.getElementById('risk-body').innerHTML=rows.map(r=>{ const d=decisionFor(r.id); return `<tr>
        <td data-label="环节 / 编号"><div class="risk-id">${esc(r.id.toUpperCase())} · ${esc(r.priority)}</div>${esc(r.phase)}</td>
        <td data-label="风控"><div class="risk-name">${esc(r.name)}</div><div style="margin-top:6px"><span class="badge ${esc(r.type)}">${esc(TYPE_LABEL[r.type])}</span></div><div class="source">${esc(r.source)}</div></td>
        <td data-label="在防什么">${esc(r.threat)}</td><td data-label="机制 / 触发行为">${esc(r.control)}<div class="source">触发：${esc(r.response)}</div></td>
        <td data-label="现阶段会不会发生"><span class="likelihood ${esc(r.likelihood)}">${esc(LIKELIHOOD_LABEL[r.likelihood])}</span><div class="risk-now">${esc(r.now)}</div></td>
        <td data-label="残余风险">${esc(r.residual)}</td><td data-label="会议结论"><div class="decision-cell"><span class="decision-line status-${esc(d.status)}">${esc(STATUS_LABEL[d.status])}</span>${d.owner?`<span class="source">Owner ${esc(d.owner)}</span>`:''}<button class="review-trigger" onclick="openReview('${r.id}')">审阅 · ${commentsFor(r.id).length}</button></div></td></tr>`;}).join('') || '<tr><td colspan="7" class="empty">没有符合筛选条件的风控项</td></tr>';
    }

    function renderQuestions() {
      document.getElementById('view-questions').innerHTML=`<div class="section-head"><div><h2>需要两个人共同拍板的问题</h2><p>这些问题来自当前代码中的双路径、fail-open 与责任边界，不是泛泛的架构题。</p></div></div><div class="question-grid">${CATALOG.questions.map(q=>`<article class="question"><div class="layer"><div class="risk-id">${esc(q.id.toUpperCase())}</div></div><h3>${esc(q.title)}</h3><p>${esc(q.question)}</p><div class="why">为什么现在要谈：${esc(q.why)}</div><div>${reviewButton(q.id)}</div></article>`).join('')}</div>`;
    }

    function renderMinutes() {
      const decided=shared.decisions.filter(x=>x.status!=='pending');
      const pending=CATALOG.risks.filter(x=>decisionFor(x.id).status==='pending' && ['gap','monitor'].includes(x.type));
      document.getElementById('view-minutes').innerHTML=`<div class="section-head"><div><h2>共享会议纪要</h2><p>由各审阅项的共同结论与追加记录自动汇总。</p></div><button class="btn" onclick="exportMarkdown()">导出 Markdown</button></div><div class="minutes"><div class="panel"><h3>已形成结论 · ${decided.length}</h3><div class="minute-group">${decided.map(d=>{const i=itemMap[d.item_id]||{title:d.item_id,kind:'项'};return `<div class="minute-item"><strong>${esc(i.kind)} · ${esc(i.title)}</strong> <span class="decision-line status-${esc(d.status)}">${esc(STATUS_LABEL[d.status])}</span><p>${esc(d.rationale||'未填写结论说明')}${d.owner?'\n负责人：'+esc(d.owner):''}</p><button class="review-trigger" onclick="openReview('${esc(d.item_id)}')">查看记录</button></div>`}).join('')||'<div class="empty">会议还没有保存共同结论</div>'}</div></div><div class="panel"><h3>优先未决 · ${pending.length}</h3><div class="minute-group">${pending.slice(0,20).map(r=>`<div class="minute-item"><strong>${esc(r.id.toUpperCase())} · ${esc(r.name)}</strong><p>${esc(r.now)}</p><button class="review-trigger" onclick="openReview('${r.id}')">开始审阅</button></div>`).join('')||'<div class="empty">当前缺口/监控项均已有结论</div>'}</div></div></div>`;
    }

    function renderAll() { renderOverview(); renderFlows(); renderBoundaries(); renderRisks(true); renderQuestions(); renderMinutes(); }
    function showView(name) { document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.view===name)); document.querySelectorAll('.view').forEach(x=>x.classList.toggle('active',x.id==='view-'+name)); if(name==='minutes') renderMinutes(); window.scrollTo({top:0,behavior:'smooth'}); }

    async function api(path, options={}) {
      if(!API_KEY) throw new Error('请先连接 API Key');
      const headers={'Authorization':'Bearer '+API_KEY, ...(options.body?{'Content-Type':'application/json'}:{})};
      const response=await fetch(API_BASE+path,{...options,headers:{...headers,...(options.headers||{})}});
      let body={}; try{ body=await response.json(); }catch(e){}
      if(!response.ok || body.code!==0) throw new Error(body.message||body.detail||`HTTP ${response.status}`);
      return body.data;
    }
    async function connectNotes(silent=false) {
      const key=document.getElementById('api-key').value.trim(); if(key){ API_KEY=key; localStorage.setItem('qmt_api_key',key); }
      const reviewer=document.getElementById('reviewer').value.trim(); if(reviewer){ REVIEWER=reviewer; localStorage.setItem('qmt_review_reviewer',reviewer); }
      if(!API_KEY){ if(!silent) toast('请输入 API Key'); return; }
      try { const snap=await api('/admin/architecture-review/session'); shared=snap; setSync(true); renderAll(); if(activeItemId&&!drawerDirty) fillDrawer(activeItemId); if(!silent) toast('共享批注已连接'); }
      catch(error){ setSync(false,error.message); if(!silent) toast(error.message); }
    }
    function setSync(ok,message='') { document.getElementById('sync-dot').className='dot '+(ok?'ok':'bad'); document.getElementById('sync-text').textContent=ok?'共享批注已连接 · 每 10 秒同步':('批注未连接'+(message?' · '+message:'')); document.getElementById('session-rev').textContent=shared.updated_at?('last '+new Date(shared.updated_at).toLocaleString()):'no notes yet'; }

    function openReview(id) { activeItemId=id; drawerDirty=false; fillDrawer(id); document.getElementById('overlay').classList.add('open'); document.body.style.overflow='hidden'; }
    function fillDrawer(id) { const item=itemMap[id]||{title:id,kind:'项'}; const d=decisionFor(id); document.getElementById('drawer-id').textContent=`${item.kind} / ${id.toUpperCase()}`; document.getElementById('drawer-title').textContent=item.title; document.getElementById('decision-status').value=d.status; document.getElementById('decision-owner').value=d.owner||''; document.getElementById('decision-rationale').value=d.rationale||''; const comments=commentsFor(id); document.getElementById('drawer-comments').innerHTML=comments.map(c=>`<div class="comment"><div class="comment-meta"><b>${esc(c.author)}</b><span>${esc(new Date(c.created_at).toLocaleString())}</span></div><div class="comment-body">${esc(c.body)}</div></div>`).join('')||'<div class="empty">还没有讨论记录</div>'; drawerDirty=false; }
    function closeDrawer(){ document.getElementById('overlay').classList.remove('open'); document.body.style.overflow=''; activeItemId=null; drawerDirty=false; }
    async function saveDecision(){ const by=reviewerRequired(); if(!by||!activeItemId)return; try{ await api(`/admin/architecture-review/decisions/${encodeURIComponent(activeItemId)}`,{method:'PUT',body:JSON.stringify({status:document.getElementById('decision-status').value,rationale:document.getElementById('decision-rationale').value,owner:document.getElementById('decision-owner').value,updated_by:by})}); drawerDirty=false; await connectNotes(true); fillDrawer(activeItemId); toast('共同结论已保存'); }catch(e){toast(e.message);} }
    async function addComment(){ const author=reviewerRequired(), body=document.getElementById('comment-body').value.trim(); if(!author||!activeItemId)return; if(!body){toast('请填写讨论记录');return;} try{ await api('/admin/architecture-review/comments',{method:'POST',body:JSON.stringify({item_id:activeItemId,author,body})}); document.getElementById('comment-body').value=''; await connectNotes(true); fillDrawer(activeItemId); toast('记录已追加'); }catch(e){toast(e.message);} }

    function exportMarkdown(){
      const lines=[`# ${CATALOG.meta.title}`,``,`日期：${CATALOG.meta.as_of}`,`批注同步：${shared.updated_at||'未连接/暂无'}`,``,`## 会议结论`];
      const decisions=shared.decisions.filter(d=>d.status!=='pending');
      if(!decisions.length) lines.push('', '暂无已保存结论。');
      decisions.forEach(d=>{const item=itemMap[d.item_id]||{title:d.item_id,kind:'项'}; lines.push('',`### ${item.kind} · ${item.title} (${d.item_id})`,'',`- 状态：${STATUS_LABEL[d.status]||d.status}`,`- 负责人：${d.owner||'未定'}`,`- 结论：${d.rationale||'未填写'}`); const comments=commentsFor(d.item_id); if(comments.length){lines.push('- 讨论记录：');comments.forEach(c=>lines.push(`  - ${c.author} / ${c.created_at}：${c.body.replace(/\n/g,' ')}`));}});
      lines.push('', '## 未决 P0 / 当前缺口'); CATALOG.risks.filter(r=>r.priority==='P0'&&decisionFor(r.id).status==='pending').forEach(r=>lines.push('',`- **${r.id.toUpperCase()} ${r.name}**：${r.now}（${r.source}）`));
      const blob=new Blob([lines.join('\n')],{type:'text/markdown;charset=utf-8'}), url=URL.createObjectURL(blob), a=document.createElement('a'); a.href=url; a.download=`server-architecture-review-${CATALOG.meta.as_of}.md`; a.click(); URL.revokeObjectURL(url);
    }

    document.getElementById('api-key').value=API_KEY; document.getElementById('reviewer').value=REVIEWER; renderAll();
    if(API_KEY) connectNotes(true);
    setInterval(()=>{ if(API_KEY) connectNotes(true); },10000);
    document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDrawer();});
  </script>
</body>
</html>"""


_REVIEW_HTML = _REVIEW_HTML_TEMPLATE.replace(
    "__REVIEW_CATALOG__",
    json.dumps(REVIEW_CATALOG, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"),
)


@router.get("/dashboard/review", response_class=HTMLResponse, include_in_schema=False)
async def architecture_review_page():
    return HTMLResponse(content=_REVIEW_HTML)


@router.get(
    "/admin/architecture-review/session",
    response_model=APIResponse[ArchitectureReviewSessionData],
    dependencies=[Depends(verify_api_key)],
)
async def architecture_review_session(
    service: ArchitectureReviewService = Depends(get_architecture_review_service),
):
    return APIResponse[ArchitectureReviewSessionData](
        code=0, message="ok", data=service.snapshot(),
    )


@router.post(
    "/admin/architecture-review/comments",
    response_model=APIResponse[ArchitectureReviewCommentItem],
    dependencies=[Depends(verify_api_key)],
)
async def add_architecture_review_comment(
    payload: ArchitectureReviewCommentCreate,
    service: ArchitectureReviewService = Depends(get_architecture_review_service),
):
    try:
        data = service.add_comment(payload)
    except ValueError as exc:
        raise APIError(ErrorCode.BAD_REQUEST, str(exc), http_status=404) from exc
    return APIResponse[ArchitectureReviewCommentItem](code=0, message="ok", data=data)


@router.put(
    "/admin/architecture-review/decisions/{item_id}",
    response_model=APIResponse[ArchitectureReviewDecisionItem],
    dependencies=[Depends(verify_api_key)],
)
async def upsert_architecture_review_decision(
    item_id: str,
    payload: ArchitectureReviewDecisionUpsert,
    service: ArchitectureReviewService = Depends(get_architecture_review_service),
):
    try:
        data = service.upsert_decision(item_id, payload)
    except ValueError as exc:
        raise APIError(ErrorCode.BAD_REQUEST, str(exc), http_status=404) from exc
    return APIResponse[ArchitectureReviewDecisionItem](code=0, message="ok", data=data)
