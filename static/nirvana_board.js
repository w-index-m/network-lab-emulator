/*
 * ニルヴァーナのボード(Grafana が持つ機能を /cyber-view の見た目のまま持たせる)
 *   BOARD   … ダッシュボード: パネル(時系列/数値/メーター/棒/表/ログ/ヒートマップ/テキスト)、変数、時間範囲、
 *             自動更新、注釈、編集(ドラッグで移動・角でサイズ変更・パネル編集)、保存・取り込み・書き出し
 *   EXPLORE … 問い合わせ: PromQL(ヴァルハラ / 内部)・LogQL(Loki)・内部ログ、結果をパネルに追加
 *   ALERTS  … アラートルール(pending → firing)、連絡先(webhook)、状態の履歴
 * API は /api/nirvana/*(engine/nirvana_board_api.py)。外部ライブラリは使わない。
 */
(() => {
  'use strict';
  const B = window.__cvBridge || { token: () => null, setView: () => {} };
  const root = document.getElementById('nb');
  if (!root) return;

  // ── 通信 ─────────────────────────────────────
  async function req(method, path, body) {
    const h = { 'Content-Type': 'application/json' };
    const tk = B.token();
    if (tk) h['X-Session-Token'] = tk;
    const r = await fetch(path, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) });
    let d = null;
    try { d = await r.json(); } catch (e) { d = null; }
    if (!r.ok) { const e = new Error((d && (d.error || d.detail)) || `HTTP ${r.status}`); e.status = r.status; throw e; }
    return d;
  }
  const get = (p) => req('GET', p);
  const qs = (o) => Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const $ = (sel, el = root) => el.querySelector(sel);
  const $$ = (sel, el = root) => [...el.querySelectorAll(sel)];
  const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* 保存できなくても動く */ } };

  // ── 見た目 ─────────────────────────────────────
  const css = `
  #nb { display: none; position: fixed; left: 0; right: 0; top: 52px; bottom: 0; z-index: 3; color: var(--ink); font-size: 12px; }
  body.board #nb { display: block; }
  #nb canvas { position: static; inset: auto; width: 100%; height: 100%; display: block; cursor: crosshair; }
  #nb .bar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 6px 16px; }
  #nb .bar .ttl { font-size: 15px; letter-spacing: .22em; color: #fff; text-shadow: 0 0 8px rgba(51,214,255,.8); margin-right: 6px; }
  #nb select, #nb input, #nb textarea, #nb button { background: rgba(4,14,30,.85); color: var(--ink); border: 1px solid var(--line);
    font: inherit; padding: 3px 7px; border-radius: 2px; }
  #nb button { cursor: pointer; color: #8fe9ff; letter-spacing: .06em; }
  #nb button:hover { background: rgba(51,214,255,.16); border-color: var(--cyan); }
  #nb button.pri { background: var(--cyan); color: #02060f; font-weight: 700; border-color: var(--cyan); }
  #nb button.warn { color: #ffb0b8; border-color: rgba(255,59,78,.6); }
  #nb button.on { color: #02060f; background: #ffab2e; border-color: #ffab2e; }
  #nb .var { color: var(--dim); } #nb .var b { color: #8fe9ff; margin-right: 4px; font-weight: 600; }
  body.board #status > div:not(.brand) { display: none; }
  #nb .scroll, #nb .pane { background: linear-gradient(180deg, rgba(2,8,18,.55), rgba(2,8,18,.72)); backdrop-filter: blur(2px); }
  #nbBoard, #nbExplore, #nbAlerts { position: absolute; inset: 0; flex-direction: column; }
  #nb .scroll { position: relative; flex: 1; min-height: 0; overflow: auto; padding: 4px 16px 24px; }
  #nb .grid { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); grid-auto-rows: 44px; gap: 10px; position: relative; }
  #nb.editing .grid { background-image: linear-gradient(rgba(51,214,255,.06) 1px, transparent 1px); background-size: 100% 54px; }
  /* ホログラムのパネル: 角を切り落とした枠 + 角の括弧 + 走査線 */
  .nbp { position: relative; display: flex; flex-direction: column; min-width: 0; min-height: 0;
    background: linear-gradient(180deg, rgba(8,28,54,.9), rgba(3,12,26,.93)); border: 1px solid rgba(51,214,255,.32);
    clip-path: polygon(10px 0, 100% 0, 100% calc(100% - 10px), calc(100% - 10px) 100%, 0 100%, 0 10px);
    box-shadow: inset 0 0 22px rgba(51,214,255,.08); }
  .nbp::before { content: ''; position: absolute; inset: 0; pointer-events: none;
    background: repeating-linear-gradient(0deg, rgba(51,214,255,.035) 0 1px, transparent 1px 3px); }
  .nbp::after { content: ''; position: absolute; left: 0; top: 0; width: 22px; height: 22px; pointer-events: none;
    border-left: 2px solid var(--cyan); border-top: 2px solid var(--cyan); }
  .nbp.alerting { border-color: rgba(255,59,78,.7); box-shadow: inset 0 0 26px rgba(255,59,78,.18); }
  .nbp .hd { display: flex; align-items: center; gap: 6px; padding: 4px 8px 2px 14px; min-height: 22px; }
  .nbp .hd .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; letter-spacing: .14em;
    color: #cdf3ff; font-size: 11px; text-transform: uppercase; }
  .nbp .hd .st { color: #ffab2e; font-size: 10px; } .nbp .hd .st.err { color: #ff6b7a; }
  .nbp .hd .tools { display: none; gap: 2px; } .nbp:hover .hd .tools, #nb.editing .nbp .hd .tools { display: flex; }
  .nbp .hd .tools span { cursor: pointer; padding: 0 4px; color: #6fbfe0; } .nbp .hd .tools span:hover { color: #fff; }
  #nb.editing .nbp .hd { cursor: move; }
  .nbp .bd { flex: 1; min-height: 0; position: relative; padding: 0 8px 6px; overflow: hidden; }
  .nbp .lg { padding: 0 10px 6px 14px; max-height: 40%; overflow: auto; font-size: 11px; white-space: nowrap; }
  .nbp .lg span { display: inline-block; margin-right: 12px; cursor: pointer; color: #9fc8dc; } .nbp .lg span.off { opacity: .35; }
  .nbp .lg i { display: inline-block; width: 10px; height: 3px; vertical-align: middle; margin-right: 4px; box-shadow: 0 0 6px currentColor; }
  .nbp .rs { position: absolute; right: 0; bottom: 0; width: 16px; height: 16px; cursor: nwse-resize; display: none;
    background: linear-gradient(135deg, transparent 50%, var(--cyan) 50%); }
  #nb.editing .nbp .rs { display: block; }
  .nbp .stat { position: absolute; inset: 0 8px 6px; display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .nbp .stat .v { font-size: clamp(28px, 4.2vw, 44px); font-weight: 700; letter-spacing: .04em; text-shadow: 0 0 14px currentColor; z-index: 1; }
  .nbp .stat .s { color: var(--dim); font-size: 11px; z-index: 1; }
  #nb .nbp .stat canvas { position: absolute; inset: 0; opacity: .35; }
  .nbp table { width: 100%; border-collapse: collapse; font-size: 11px; }
  .nbp th { text-align: left; color: #6fbfe0; font-weight: 600; border-bottom: 1px solid rgba(51,214,255,.3); padding: 2px 6px; position: sticky; top: 0; background: rgba(4,16,32,.95); }
  .nbp td { padding: 2px 6px; border-bottom: 1px solid rgba(51,214,255,.08); white-space: nowrap; }
  .nbp .tbl { position: absolute; inset: 0 8px 6px; overflow: auto; }
  .nbp .logs { position: absolute; inset: 0 8px 6px; overflow: auto; font-size: 11px; }
  .nbp .logs div { white-space: pre-wrap; word-break: break-all; padding: 1px 0; border-bottom: 1px solid rgba(51,214,255,.06); }
  .nbp .logs .ts { color: var(--dim); margin-right: 8px; } .nbp .logs .h { color: #8fe9ff; margin-right: 8px; }
  .lv-error, .lv-crit, .lv-alert, .lv-emerg { color: #ff6b7a; } .lv-warning { color: #ffab2e; } .lv-info, .lv-notice { color: #bfe9ff; }
  .nbp .txt { position: absolute; inset: 0 12px 8px; overflow: auto; white-space: pre-wrap; line-height: 1.6; }
  .nbp .empty { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--dim); }
  #nbTip { position: fixed; z-index: 20; pointer-events: none; display: none; background: rgba(2,10,22,.94); border: 1px solid var(--cyan);
    padding: 6px 9px; font-size: 11px; color: #dff6ff; box-shadow: 0 0 16px rgba(51,214,255,.35); max-width: 420px; }
  #nbTip i { display: inline-block; width: 9px; height: 3px; vertical-align: middle; margin-right: 5px; }
  #nbModal { position: fixed; inset: 0; z-index: 30; display: none; background: rgba(0,4,10,.6); }
  #nbModal.show { display: block; }
  #nbModal .win { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); width: min(1100px, calc(100vw - 24px));
    max-height: calc(100vh - 40px); overflow: auto; background: rgba(4,14,30,.97); border: 1px solid var(--cyan);
    box-shadow: 0 0 40px rgba(51,214,255,.3); padding: 12px 16px;
    clip-path: polygon(14px 0, 100% 0, 100% calc(100% - 14px), calc(100% - 14px) 100%, 0 100%, 0 14px); }
  #nbModal h3 { margin: 0 0 10px; letter-spacing: .2em; color: #fff; font-size: 14px; }
  #nbModal label { display: inline-flex; flex-direction: column; gap: 2px; margin: 0 10px 8px 0; color: var(--dim); font-size: 11px; vertical-align: top; }
  #nbModal textarea { width: 100%; min-height: 56px; font-family: inherit; }
  #nbModal .row { display: flex; flex-wrap: wrap; align-items: flex-end; }
  #nbModal .q { border-left: 2px solid var(--cyan); padding-left: 8px; margin: 6px 0; }
  #nbModal .prev { height: 220px; position: relative; margin: 8px 0; }
  #nbModal .act { display: flex; gap: 8px; justify-content: flex-end; margin-top: 10px; }
  #nb .toast { position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); background: rgba(4,14,30,.95); border: 1px solid var(--cyan);
    padding: 6px 14px; z-index: 40; display: none; } #nb .toast.err { border-color: #ff3b4e; color: #ffb0b8; }
  #nb .pane { position: relative; flex: 1; min-height: 0; overflow: auto; padding: 4px 16px 24px; }
  #nb .exq { display: flex; gap: 8px; align-items: stretch; }
  #nb .exq textarea { flex: 1; min-height: 52px; font-family: inherit; font-size: 13px; color: #fff; }
  #nb .exres { display: grid; grid-template-columns: 1fr; gap: 10px; margin-top: 10px; }
  #nb .hist span { display: inline-block; margin: 4px 6px 0 0; padding: 1px 6px; border: 1px solid rgba(51,214,255,.25); cursor: pointer; color: #9fc8dc; }
  #nb .badge { display: inline-block; min-width: 64px; text-align: center; padding: 1px 6px; font-weight: 700; letter-spacing: .1em; font-size: 10px; }
  #nb .b-normal { background: rgba(93,255,168,.15); color: #5dffa8; border: 1px solid #5dffa8; }
  #nb .b-pending { background: rgba(255,171,46,.15); color: #ffab2e; border: 1px solid #ffab2e; }
  #nb .b-firing { background: rgba(255,59,78,.2); color: #ff6b7a; border: 1px solid #ff3b4e; animation: nbblink 1s infinite; }
  #nb .b-error, #nb .b-off { background: rgba(120,120,140,.2); color: #b8c0d0; border: 1px solid #667; }
  @keyframes nbblink { 50% { opacity: .55; } }
  #nb .sec { margin: 14px 0 6px; letter-spacing: .25em; color: #8fe9ff; font-size: 12px; border-bottom: 1px solid rgba(51,214,255,.25); padding-bottom: 3px; }
  #nb .ar { display: grid; grid-template-columns: 90px 1.4fr 2fr 1fr auto; gap: 8px; align-items: center; padding: 6px 4px;
    border-bottom: 1px solid rgba(51,214,255,.08); }
  #nb .ar .x { color: var(--dim); font-size: 11px; word-break: break-all; }
  @media (max-width: 640px) {
    #nb { top: 160px; }
    #nb .grid { grid-template-columns: 1fr; grid-auto-rows: 40px; }
    #nb .grid .nbp { grid-column: 1 / -1 !important; grid-row: auto / span var(--h, 4) !important; }
    #nb .bar { padding: 4px 10px; gap: 4px; }
    #nb .ar { grid-template-columns: 76px 1fr; }
  }`;
  const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
  const COLORS = ['#33d6ff', '#5dffa8', '#ffab2e', '#c58bff', '#ff6b9a', '#ffe066', '#6be4ff', '#ff3b4e', '#9dff5d', '#ff9f6b'];
  const RANGES = [['now-5m', '5分'], ['now-15m', '15分'], ['now-1h', '1時間'], ['now-3h', '3時間'], ['now-6h', '6時間'], ['now-12h', '12時間'], ['now-24h', '24時間'], ['now-2d', '2日'], ['now-7d', '7日']];
  const REFRESH = [['', '自動更新なし'], ['5s', '5秒'], ['10s', '10秒'], ['30s', '30秒'], ['1m', '1分'], ['5m', '5分']];
  const UNITS = [['none', 'なし'], ['short', '短縮(k,M)'], ['bps', 'bps'], ['Bps', 'B/s'], ['bytes', 'bytes'], ['percent', '% (0-100)'], ['percentunit', '% (0-1)'], ['s', '秒'], ['ms', 'ms'], ['pps', 'pps']];
  const TYPES = [['timeseries', '時系列'], ['stat', '数値'], ['gauge', 'メーター'], ['bar', '棒(バーゲージ)'], ['table', '表'], ['heatmap', 'ヒートマップ'], ['logs', 'ログ'], ['text', 'テキスト']];
  const durSec = (s) => { const m = /^(\d+)(s|m|h|d)$/.exec(s || ''); return m ? +m[1] * { s: 1, m: 60, h: 3600, d: 86400 }[m[2]] : 0; };
  const relSec = (r) => (r === 'now' ? 0 : durSec(String(r).replace('now-', '')));

  // 単位つきの表示
  function fmt(v, unit, dec) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    if (!Number.isFinite(v)) return v > 0 ? '∞' : '-∞';
    const d = (x, n) => (dec !== undefined && dec !== null && dec !== '' ? x.toFixed(+dec) : (Math.abs(x) >= 100 ? x.toFixed(0) : Math.abs(x) >= 10 ? x.toFixed(1) : x.toFixed(n ?? 2)));
    const si = (x, base, units) => { let i = 0; let a = Math.abs(x); while (a >= base && i < units.length - 1) { a /= base; x /= base; i++; } return d(x) + ' ' + units[i]; };
    switch (unit) {
      case 'bps': return si(v, 1000, ['bps', 'kbps', 'Mbps', 'Gbps', 'Tbps']);
      case 'Bps': return si(v, 1000, ['B/s', 'kB/s', 'MB/s', 'GB/s']);
      case 'bytes': return si(v, 1024, ['B', 'KiB', 'MiB', 'GiB', 'TiB']);
      case 'pps': return si(v, 1000, ['pps', 'kpps', 'Mpps']);
      case 'percent': return d(v, 1) + '%';
      case 'percentunit': return d(v * 100, 1) + '%';
      case 's': return v >= 3600 ? d(v / 3600, 1) + ' h' : v >= 60 ? d(v / 60, 1) + ' min' : d(v, 1) + ' s';
      case 'ms': return v >= 1000 ? d(v / 1000, 2) + ' s' : d(v, 1) + ' ms';
      case 'none': return Number.isInteger(v) && (dec === undefined || dec === null || dec === '') ? String(v) : d(v);
      case 'short': return si(v, 1000, ['', 'k', 'M', 'G', 'T']).trim();
      default: return Number.isInteger(v) && (dec === undefined || dec === null || dec === '') ? String(v) : d(v);
    }
  }
  const legendOf = (tmpl, labels) => {
    if (tmpl) return tmpl.replace(/\{\{\s*([\w]+)\s*\}\}/g, (_, k) => labels[k] ?? '');
    const ks = Object.keys(labels).filter((k) => k !== '__name__');
    const name = labels.__name__ || '';
    return ks.length ? `${name}{${ks.map((k) => `${k}="${labels[k]}"`).join(', ')}}` : (name || '値');
  };
  function thrColor(v, thresholds, dflt) {
    let c = dflt || '#33d6ff';
    for (const t of thresholds || []) if (t.value === null || t.value === undefined || t.value === '' || v >= +t.value) c = t.color;
    return c;
  }
  const mapVal = (v, mappings) => (mappings && v !== null && v !== undefined && mappings[String(Math.round(v * 1e6) / 1e6)] !== undefined ? mappings[String(Math.round(v * 1e6) / 1e6)] : null);
  const fmtTime = (t, span) => { const d = new Date(t * 1000); const p = (n) => String(n).padStart(2, '0');
    return span > 2 * 86400 ? `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}` : `${p(d.getHours())}:${p(d.getMinutes())}${span < 900 ? ':' + p(d.getSeconds()) : ''}`; };

  // ── 状態 ─────────────────────────────────────
  const S = {
    view: null, ds: [], dsDefault: 'internal', list: [], dash: null, dirty: false, edit: false,
    range: { from: 'now-1h', to: 'now' }, refresh: '', vars: {}, varOpts: {}, timer: null, hoverT: null,
    anns: [], panels: {}, hidden: {}, available: true,
  };

  // ── 骨組み ─────────────────────────────────────
  root.innerHTML = `
  <div id="nbBoard" style="display:none">
    <div class="bar">
      <span class="ttl">BOARD</span>
      <select id="nbDash" aria-label="ダッシュボード"></select>
      <span id="nbVars"></span>
      <span style="flex:1"></span>
      <select id="nbRange" aria-label="時間範囲">${RANGES.map(([v, n]) => `<option value="${v}">直近${n}</option>`).join('')}<option value="custom">期間を指定…</option></select>
      <select id="nbRefresh" aria-label="自動更新">${REFRESH.map(([v, n]) => `<option value="${v}">${n}</option>`).join('')}</select>
      <button id="nbReload" title="読み込み直す">⟳</button>
      <button id="nbAddAnn" title="今の時刻に注釈を付ける(時系列パネルを Ctrl+クリックでその時刻に付けられます)">✎ 注釈</button>
      <button id="nbEdit" title="編集(ドラッグで移動・右下の角でサイズ変更)">編集</button>
      <button id="nbAddPanel" title="パネルを追加">＋ パネル</button>
      <button id="nbSave" class="pri" title="保存">保存</button>
      <button id="nbMore" title="新規・複製・設定・取り込み・書き出し・削除">…</button>
    </div>
    <div class="scroll"><div class="grid" id="nbGrid"></div></div>
  </div>
  <div id="nbExplore" style="display:none">
    <div class="bar"><span class="ttl">EXPLORE</span>
      <select id="exDs" aria-label="データソース"></select>
      <select id="exKind" aria-label="種類"><option value="metrics">時系列</option><option value="logs">ログ</option></select>
      <select id="exRange" aria-label="時間範囲">${RANGES.map(([v, n]) => `<option value="${v}">直近${n}</option>`).join('')}</select>
      <span style="flex:1"></span><button id="exToPanel" title="今の問い合わせをダッシュボードのパネルにする">＋ ダッシュボードへ</button></div>
    <div class="pane">
      <div class="exq"><textarea id="exExpr" spellcheck="false" placeholder="例) sum by (host_a) (netlab_link_bps)   ・ Shift+Enter で実行"></textarea>
        <button id="exRun" class="pri" style="min-width:90px">実行 ▶</button></div>
      <datalist id="exNames"></datalist>
      <div class="hist" id="exHist"></div>
      <div class="exres" id="exRes"></div>
    </div>
  </div>
  <div id="nbAlerts" style="display:none">
    <div class="bar"><span class="ttl">ALERTS</span><span id="alSum" class="var"></span><span style="flex:1"></span>
      <button id="alNew" class="pri">＋ ルール</button><button id="alNewCp">＋ 連絡先</button></div>
    <div class="pane" id="alPane"></div>
  </div>
  <div id="nbModal"><div class="win" id="nbWin"></div></div>
  <div class="toast" id="nbToast"></div>`;
  const tip = document.createElement('div'); tip.id = 'nbTip'; document.body.appendChild(tip);
  let toastTimer = null;
  function toast(msg, err) {
    const t = $('#nbToast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.style.display = 'block';
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.style.display = 'none'; }, 3200);
  }
  function modal(html, onReady) { $('#nbWin').innerHTML = html; $('#nbModal').classList.add('show'); if (onReady) onReady($('#nbWin')); }
  function closeModal() { $('#nbModal').classList.remove('show'); $('#nbWin').innerHTML = ''; }
  $('#nbModal').addEventListener('mousedown', (e) => { if (e.target.id === 'nbModal') closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('#nbModal').classList.contains('show')) closeModal(); });

  // ── 時間範囲 ───────────────────────────────────
  function rangeNow() {
    const now = Date.now() / 1000;
    const end = /^\d/.test(S.range.to) ? +S.range.to : now - relSec(S.range.to);
    const start = /^\d/.test(S.range.from) ? +S.range.from : now - relSec(S.range.from);
    return { start, end, step: Math.max(5, Math.ceil((end - start) / 240)) };
  }

  // ── 変数 ─────────────────────────────────────
  async function loadVarOptions() {
    const vars = (S.dash && S.dash.variables) || [];
    for (const v of vars) {
      let opts = [];
      try {
        if (v.type === 'custom') opts = String(v.query || '').split(',').map((x) => x.trim()).filter(Boolean);
        else {
          const m = /^label_values\(\s*(?:([\w:]+(?:\{[^}]*\})?)\s*,\s*)?(\w+)\s*\)$/.exec(interp((v.query || '').trim()));
          if (m) opts = (await get('/api/nirvana/labels?' + qs({ ds: v.ds || S.dsDefault, label: m[2], metric: m[1] || '' }))).values;
        }
      } catch (e) { opts = []; }
      S.varOpts[v.name] = opts;
      const saved = S.vars[v.name] ?? v.current ?? (v.includeAll ? ['$__all'] : opts.slice(0, 1));
      S.vars[v.name] = Array.isArray(saved) ? saved : [saved];
    }
    renderVars();
  }
  function renderVars() {
    const vars = (S.dash && S.dash.variables) || [];
    $('#nbVars').innerHTML = vars.map((v) => {
      const opts = (v.includeAll ? [['$__all', 'すべて']] : []).concat((S.varOpts[v.name] || []).map((o) => [o, o]));
      return `<span class="var"><b>${esc(v.label || v.name)}</b><select data-var="${esc(v.name)}" ${v.multi ? 'multiple size="1"' : ''}>${opts.map(([o, n]) =>
        `<option value="${esc(o)}" ${(S.vars[v.name] || []).includes(o) ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select></span>`;
    }).join(' ');
    $$('#nbVars select').forEach((sel) => { sel.onchange = () => { S.vars[sel.dataset.var] = [...sel.selectedOptions].map((o) => o.value); refreshAll(); }; });
  }
  const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  function interp(s, step) {
    if (!s) return s;
    const { start, end } = rangeNow();
    let out = s.replace(/\$__interval\b/g, `${step || 60}s`).replace(/\$__rate_interval\b/g, `${Math.max(60, 4 * (step || 15))}s`)
      .replace(/\$__range\b/g, `${Math.round(end - start)}s`);
    for (const v of (S.dash && S.dash.variables) || []) {
      const sel = S.vars[v.name] || [];
      const vals = sel.includes('$__all') ? null : sel;
      const rep = vals === null ? '.*' : vals.length > 1 ? `(${vals.map(reEsc).join('|')})` : (vals[0] ?? '');
      out = out.replace(new RegExp(`\\$\\{${v.name}\\}|\\$${v.name}\\b`, 'g'), rep);
    }
    return out;
  }

  // ── パネル: データ取得 ──────────────────────────────
  async function fetchPanel(p) {
    const r = rangeNow();
    if (p.type === 'text') return { series: [], logs: [] };
    if (p.type === 'logs') {
      const t = (p.targets || [])[0] || {};
      const d = await get('/api/nirvana/logs?' + qs({ ds: p.ds || 'internal', expr: interp(t.expr || '', r.step), start: r.start, end: r.end, limit: 300 }));
      return { series: [], logs: d.logs };
    }
    const all = [];
    for (const t of p.targets || []) {
      if (!t.expr || t.hide) continue;
      const d = await get('/api/nirvana/query?' + qs({ ds: p.ds || S.dsDefault, expr: interp(t.expr, r.step), start: r.start, end: r.end, step: r.step }));
      for (const s of d.series) all.push({ name: legendOf(t.legend, s.labels), labels: s.labels, points: s.points });
    }
    return { series: all, logs: [] };
  }
  function reduce(points, how) {
    const v = points.map((p) => p[1]).filter((x) => x !== null && Number.isFinite(x));
    if (!v.length) return null;
    return { last: v[v.length - 1], avg: v.reduce((a, b) => a + b, 0) / v.length, max: Math.max(...v), min: Math.min(...v), sum: v.reduce((a, b) => a + b, 0) }[how || 'last'];
  }

  // ── パネル: 描画 ───────────────────────────────────
  function setupCanvas(cv) {
    const r = cv.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1;
    cv.width = Math.max(10, Math.round(r.width * dpr)); cv.height = Math.max(10, Math.round(r.height * dpr));
    const c = cv.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0); return { c, w: r.width, h: r.height };
  }
  function drawTimeseries(cv, data, p, range, opt = {}) {
    const { c, w, h } = setupCanvas(cv); const o = p.options || {};
    const L = 72, R = 10, T = 8, Bm = 18, pw = w - L - R, ph = h - T - Bm;
    c.clearRect(0, 0, w, h);
    const hid = S.hidden[p.id] || {};
    const ser = data.series.map((s, i) => ({ ...s, col: COLORS[i % COLORS.length] })).filter((s) => !hid[s.name]);
    let lo = Infinity, hi = -Infinity;
    for (const s of ser) for (const [, v] of s.points) if (Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (o.stack) {
      const acc = {}; for (const s of ser) s.points = s.points.map(([t, v]) => { acc[t] = (acc[t] || 0) + (v || 0); return [t, acc[t]]; });
      hi = Math.max(hi, ...Object.values(acc));
    }
    if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
    if (o.min !== undefined && o.min !== '' && o.min !== null) lo = +o.min; else lo = Math.min(0, lo);
    if (o.max !== undefined && o.max !== '' && o.max !== null) hi = +o.max;
    if (hi <= lo) hi = lo + 1;
    hi += (hi - lo) * 0.06;
    const X = (t) => L + (t - range.start) / (range.end - range.start) * pw;
    const Y = (v) => T + ph - (v - lo) / (hi - lo) * ph;
    // 格子と目盛り
    c.font = '10px monospace'; c.textBaseline = 'middle';
    for (let k = 0; k <= 4; k++) {
      const v = lo + (hi - lo) * k / 4, y = Y(v);
      c.strokeStyle = 'rgba(51,214,255,.10)'; c.beginPath(); c.moveTo(L, y); c.lineTo(L + pw, y); c.stroke();
      c.fillStyle = '#4f7896'; c.textAlign = 'right'; c.fillText(fmt(v, o.unit, o.decimals), L - 6, y);
    }
    const span = range.end - range.start, nT = Math.max(2, Math.floor(pw / 90));
    c.textAlign = 'center';
    for (let k = 0; k <= nT; k++) {
      const t = range.start + span * k / nT, x = X(t);
      c.strokeStyle = 'rgba(51,214,255,.06)'; c.beginPath(); c.moveTo(x, T); c.lineTo(x, T + ph); c.stroke();
      c.fillStyle = '#4f7896'; c.fillText(fmtTime(t, span), x, h - 8);
    }
    // しきい値の線
    for (const t of o.thresholds || []) {
      if (t.value === null || t.value === undefined || t.value === '' || +t.value < lo || +t.value > hi) continue;
      c.save(); c.setLineDash([5, 4]); c.strokeStyle = t.color; c.globalAlpha = 0.7; c.beginPath(); c.moveTo(L, Y(+t.value)); c.lineTo(L + pw, Y(+t.value)); c.stroke(); c.restore();
    }
    // 系列(光る線 + 下へ消えるグラデーション)
    for (const s of ser) {
      const pts = s.points.filter(([t, v]) => Number.isFinite(v) && t >= range.start - 1 && t <= range.end + 1);
      if (!pts.length) continue;
      c.save(); c.beginPath();
      let gap = false;
      pts.forEach(([t, v], i) => {
        const x = X(t), y = Y(v);
        if (i === 0 || gap) c.moveTo(x, y); else c.lineTo(x, y);
        gap = i < pts.length - 1 && pts[i + 1][0] - t > range.step * 3.5;
      });
      c.strokeStyle = s.col; c.lineWidth = o.lineWidth || 1.6; c.shadowColor = s.col; c.shadowBlur = 8; c.stroke();
      if (o.fill !== false && pts.length > 1) {
        c.shadowBlur = 0; c.lineTo(X(pts[pts.length - 1][0]), Y(Math.max(lo, 0))); c.lineTo(X(pts[0][0]), Y(Math.max(lo, 0))); c.closePath();
        const g = c.createLinearGradient(0, T, 0, T + ph); g.addColorStop(0, s.col + '40'); g.addColorStop(1, s.col + '00'); c.fillStyle = g; c.fill();
      }
      if (o.points || pts.length < 3) { c.shadowBlur = 0; c.fillStyle = s.col; for (const [t, v] of pts) { c.beginPath(); c.arc(X(t), Y(v), 2.2, 0, Math.PI * 2); c.fill(); } }
      c.restore();
    }
    // 注釈(縦の線と菱形)
    for (const a of S.anns) {
      if (a.time < range.start || a.time > range.end) continue;
      const x = X(a.time), col = a.kind === 'user' ? '#ffe066' : a.kind === 'rule' ? '#2ee6c0' : (a.tags || []).includes('link') ? '#ffab2e' : '#ff6b7a';
      c.save(); c.strokeStyle = col; c.globalAlpha = 0.55; c.setLineDash([2, 3]); c.beginPath(); c.moveTo(x, T); c.lineTo(x, T + ph); c.stroke(); c.setLineDash([]);
      c.globalAlpha = 1; c.fillStyle = col; c.beginPath(); c.moveTo(x, T + ph + 1); c.lineTo(x + 4, T + ph + 5); c.lineTo(x, T + ph + 9); c.lineTo(x - 4, T + ph + 5); c.closePath(); c.fill(); c.restore();
    }
    // 共有の十字線
    if (S.hoverT && S.hoverT >= range.start && S.hoverT <= range.end) {
      const x = X(S.hoverT); c.save(); c.strokeStyle = 'rgba(255,255,255,.45)'; c.beginPath(); c.moveTo(x, T); c.lineTo(x, T + ph); c.stroke();
      for (const s of ser) { const pt = nearest(s.points, S.hoverT); if (pt && Number.isFinite(pt[1])) { c.fillStyle = s.col; c.shadowColor = s.col; c.shadowBlur = 10; c.beginPath(); c.arc(X(pt[0]), Y(pt[1]), 3.2, 0, Math.PI * 2); c.fill(); } }
      c.restore();
    }
    cv._geom = { L, pw, range, ser, o, T, ph };
    if (opt.noLegend) return;
  }
  function nearest(points, t) { let best = null, bd = Infinity; for (const p of points) { const d = Math.abs(p[0] - t); if (d < bd) { bd = d; best = p; } } return best; }
  function drawHeatmap(cv, data, p, range) {
    const { c, w, h } = setupCanvas(cv); const o = p.options || {};
    const L = 90, T = 6, Bm = 16, pw = w - L - 8, ph = h - T - Bm;
    c.clearRect(0, 0, w, h);
    const ser = data.series; if (!ser.length) return;
    let lo = Infinity, hi = -Infinity;
    for (const s of ser) for (const [, v] of s.points) if (Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (o.min !== undefined && o.min !== '') lo = +o.min; if (o.max !== undefined && o.max !== '') hi = +o.max; if (hi <= lo) hi = lo + 1;
    const rowH = ph / ser.length, cols = Math.max(1, Math.round((range.end - range.start) / range.step));
    const cw = pw / cols;
    const col = (k) => { const a = [[15, 60, 110], [51, 214, 255], [255, 171, 46], [255, 59, 78]]; const f = Math.min(0.999, Math.max(0, k)) * 3, i = Math.floor(f), r = f - i;
      return `rgb(${a[i].map((x, j) => Math.round(x + (a[i + 1][j] - x) * r)).join(',')})`; };
    ser.forEach((s, i) => {
      for (const [t, v] of s.points) {
        if (!Number.isFinite(v)) continue;
        const x = L + (t - range.start) / (range.end - range.start) * pw;
        c.fillStyle = col((v - lo) / (hi - lo)); c.fillRect(x, T + i * rowH + 1, Math.max(1, cw - 1), Math.max(1, rowH - 2));
      }
      c.fillStyle = '#9fc8dc'; c.font = '10px monospace'; c.textAlign = 'right'; c.textBaseline = 'middle';
      c.fillText(s.name.slice(0, 14), L - 6, T + i * rowH + rowH / 2);
    });
    c.fillStyle = '#4f7896'; c.textAlign = 'center';
    for (let k = 0; k <= 4; k++) { const t = range.start + (range.end - range.start) * k / 4; c.fillText(fmtTime(t, range.end - range.start), L + pw * k / 4, h - 6); }
  }
  function drawGauge(cv, v, p) {
    const { c, w, h } = setupCanvas(cv); const o = p.options || {};
    const mn = +(o.min ?? 0), mx = +(o.max ?? 100) || 1, k = v === null ? 0 : Math.max(0, Math.min(1, (v - mn) / (mx - mn)));
    const cx = w / 2, cy = h * 0.62, R = Math.max(10, Math.min(w * 0.38, h * 0.55)), a0 = Math.PI * 0.8, a1 = Math.PI * 2.2;
    c.clearRect(0, 0, w, h);
    c.lineCap = 'butt';
    c.lineWidth = R * 0.18; c.strokeStyle = 'rgba(51,214,255,.12)'; c.beginPath(); c.arc(cx, cy, R, a0, a1); c.stroke();
    // しきい値の色の帯
    const th = (o.thresholds || []).filter((t) => t.value !== null && t.value !== '' && t.value !== undefined);
    let prev = mn, prevC = (o.thresholds || [])[0]?.color || '#33d6ff';
    for (const t of [...th, { value: mx, color: null }]) {
      const s0 = a0 + (a1 - a0) * Math.max(0, Math.min(1, (prev - mn) / (mx - mn))), s1 = a0 + (a1 - a0) * Math.max(0, Math.min(1, (+t.value - mn) / (mx - mn)));
      c.lineWidth = R * 0.05; c.strokeStyle = prevC; c.beginPath(); c.arc(cx, cy, R * 1.16, s0, s1); c.stroke();
      prev = +t.value; prevC = t.color || prevC;
    }
    const colr = thrColor(v ?? mn, o.thresholds, '#33d6ff');
    c.lineWidth = R * 0.18; c.strokeStyle = colr; c.shadowColor = colr; c.shadowBlur = 16;
    c.beginPath(); c.arc(cx, cy, R, a0, a0 + (a1 - a0) * k); c.stroke(); c.shadowBlur = 0;
    for (let i = 0; i <= 10; i++) { const a = a0 + (a1 - a0) * i / 10; c.strokeStyle = 'rgba(191,233,255,.4)'; c.lineWidth = 1;
      c.beginPath(); c.moveTo(cx + Math.cos(a) * R * 0.78, cy + Math.sin(a) * R * 0.78); c.lineTo(cx + Math.cos(a) * R * 0.86, cy + Math.sin(a) * R * 0.86); c.stroke(); }
    c.fillStyle = colr; c.font = `700 ${Math.max(12, R * 0.38)}px monospace`; c.textAlign = 'center'; c.textBaseline = 'middle';
    c.shadowColor = colr; c.shadowBlur = 12; c.fillText(mapVal(v, o.mappings) ?? fmt(v, o.unit, o.decimals), cx, cy); c.shadowBlur = 0;
  }
  function sparkline(cv, points, col) {
    const { c, w, h } = setupCanvas(cv); c.clearRect(0, 0, w, h);
    const v = points.filter((p) => Number.isFinite(p[1])); if (v.length < 2) return;
    const t0 = v[0][0], t1 = v[v.length - 1][0], lo = Math.min(...v.map((p) => p[1])), hi = Math.max(...v.map((p) => p[1])) || 1;
    c.beginPath(); v.forEach(([t, y], i) => { const X = (t - t0) / ((t1 - t0) || 1) * w, Y2 = h - (y - lo) / ((hi - lo) || 1) * h * 0.7 - 2; if (i) c.lineTo(X, Y2); else c.moveTo(X, Y2); });
    c.strokeStyle = col; c.lineWidth = 1.5; c.stroke(); c.lineTo(w, h); c.lineTo(0, h); c.closePath(); c.fillStyle = col + '30'; c.fill();
  }

  function renderPanelBody(el, p, data, range) {
    const bd = el.querySelector('.bd'), lg = el.querySelector('.lg'), o = p.options || {};
    bd.innerHTML = ''; lg.innerHTML = '';
    let alerting = false;
    if (p.type === 'text') { bd.innerHTML = `<div class="txt">${esc(o.content || '')}</div>`; return; }
    if (p.type === 'logs') {
      const rows = data.logs || [];
      bd.innerHTML = rows.length ? `<div class="logs">${rows.map((r) => `<div><span class="ts">${new Date(r.t * 1000).toLocaleTimeString()}</span><span class="h">${esc(r.labels.host || r.labels.hostname || r.labels.job || '')}</span><span class="lv-${esc(r.labels.level || '')}">${esc(r.line)}</span></div>`).join('')}</div>`
        : '<div class="empty">ログはありません</div>';
      return;
    }
    if (!data.series.length) {
      if (o.zeroIfEmpty && (p.type === 'stat' || p.type === 'gauge')) data = { series: [{ name: '', labels: {}, points: [[range.end, 0]] }] };
      else { bd.innerHTML = '<div class="empty">データがありません</div>'; return; }
    }
    if (p.type === 'timeseries') {
      const cv = document.createElement('canvas'); bd.appendChild(cv); drawTimeseries(cv, data, p, range); bindHover(cv, p);
      cv.addEventListener('click', (e) => { if (e.ctrlKey || e.metaKey) { const g = cv._geom; const r = cv.getBoundingClientRect(); addAnnotation(g.range.start + (e.clientX - r.left - g.L) / g.pw * (g.range.end - g.range.start)); } });
      const hid = S.hidden[p.id] || {};
      lg.innerHTML = data.series.map((s, i) => { const last = reduce(s.points, 'last'), mx = reduce(s.points, 'max');
        return `<span data-n="${esc(s.name)}" class="${hid[s.name] ? 'off' : ''}" style="color:${COLORS[i % COLORS.length]}"><i style="background:${COLORS[i % COLORS.length]}"></i><b style="color:#cfe9f5;font-weight:400">${esc(s.name)}</b> 最新 ${fmt(last, o.unit, o.decimals)} ・ 最大 ${fmt(mx, o.unit, o.decimals)}</span>`; }).join('');
      $$('span', lg).forEach((sp) => { sp.onclick = () => { const hd = S.hidden[p.id] = S.hidden[p.id] || {}; hd[sp.dataset.n] = !hd[sp.dataset.n]; renderPanelBody(el, p, data, range); }; });
      const thr = (o.thresholds || []).filter((t) => t.value !== null && t.value !== '' && t.value !== undefined);
      if (thr.length) alerting = data.series.some((s) => (reduce(s.points, 'last') ?? -Infinity) >= +thr[thr.length - 1].value);
    } else if (p.type === 'heatmap') {
      const cv = document.createElement('canvas'); bd.appendChild(cv); drawHeatmap(cv, data, p, range);
    } else if (p.type === 'stat') {
      const s = data.series[0], v = reduce(s.points, o.reduce), col = thrColor(v, o.thresholds, '#33d6ff');
      const more = data.series.length > 1 ? `<div class="s">${data.series.slice(1, 4).map((x) => `${esc(x.name)} ${fmt(reduce(x.points, o.reduce), o.unit, o.decimals)}`).join(' ・ ')}</div>` : '';
      bd.innerHTML = `<div class="stat"><canvas></canvas><div class="v" style="color:${col}">${esc(mapVal(v, o.mappings) ?? fmt(v, o.unit, o.decimals))}</div><div class="s">${esc(data.series.length === 1 ? s.name : '')}</div>${more}</div>`;
      sparkline(bd.querySelector('canvas'), s.points, col);
      alerting = col === '#ff3b4e';
    } else if (p.type === 'gauge') {
      const v = reduce(data.series[0].points, o.reduce); const cv = document.createElement('canvas'); bd.appendChild(cv); drawGauge(cv, v, p);
      alerting = thrColor(v, o.thresholds) === '#ff3b4e';
    } else if (p.type === 'bar') {
      const rows = data.series.map((s) => ({ n: s.name, v: reduce(s.points, o.reduce) })).sort((a, b) => (b.v ?? 0) - (a.v ?? 0));
      const mx = o.max !== undefined && o.max !== '' ? +o.max : Math.max(...rows.map((r) => r.v || 0), 1e-9), mn = +(o.min || 0);
      bd.innerHTML = `<div class="tbl">${rows.map((r) => { const col = thrColor(r.v, o.thresholds, '#33d6ff'), k = Math.max(0, Math.min(1, ((r.v ?? 0) - mn) / ((mx - mn) || 1)));
        return `<div style="display:grid;grid-template-columns:minmax(80px,32%) 1fr 74px;gap:6px;align-items:center;margin:3px 0">
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#9fc8dc">${esc(r.n)}</span>
          <span style="height:12px;background:rgba(51,214,255,.08);position:relative"><i style="position:absolute;left:0;top:0;bottom:0;width:${(k * 100).toFixed(1)}%;background:linear-gradient(90deg,${col}55,${col});box-shadow:0 0 8px ${col}"></i></span>
          <span style="text-align:right;color:${col}">${esc(mapVal(r.v, o.mappings) ?? fmt(r.v, o.unit, o.decimals))}</span></div>`; }).join('')}</div>`;
      alerting = rows.some((r) => thrColor(r.v, o.thresholds) === '#ff3b4e');
    } else if (p.type === 'table') {
      const keys = [...new Set(data.series.flatMap((s) => Object.keys(s.labels).filter((k) => k !== '__name__')))];
      bd.innerHTML = `<div class="tbl"><table><tr>${keys.map((k) => `<th>${esc(k)}</th>`).join('')}<th>値</th><th>時刻</th></tr>${data.series.map((s) => {
        const pt = s.points[s.points.length - 1] || [0, null], col = thrColor(pt[1], o.thresholds, '#dff6ff');
        return `<tr>${keys.map((k) => `<td>${esc(s.labels[k] ?? '')}</td>`).join('')}<td style="color:${col};font-weight:700">${esc(mapVal(pt[1], o.mappings) ?? fmt(pt[1], o.unit, o.decimals))}</td><td style="color:var(--dim)">${pt[0] ? new Date(pt[0] * 1000).toLocaleTimeString() : ''}</td></tr>`;
      }).join('')}</table></div>`;
    }
    el.classList.toggle('alerting', !!alerting);
  }

  function bindHover(cv, p) {
    cv.addEventListener('mousemove', (e) => {
      const g = cv._geom; if (!g) return;
      const r = cv.getBoundingClientRect(), x = e.clientX - r.left;
      if (x < g.L || x > g.L + g.pw) { S.hoverT = null; tip.style.display = 'none'; redrawCrosshair(); return; }
      S.hoverT = g.range.start + (x - g.L) / g.pw * (g.range.end - g.range.start);
      const rows = g.ser.map((s) => [s, nearest(s.points, S.hoverT)]).filter(([, pt]) => pt).sort((a, b) => (b[1][1] ?? 0) - (a[1][1] ?? 0));
      const ann = S.anns.filter((a) => Math.abs(a.time - S.hoverT) < (g.range.end - g.range.start) / g.pw * 4);
      tip.innerHTML = `<div style="color:var(--dim)">${new Date(S.hoverT * 1000).toLocaleString()}</div>` +
        rows.slice(0, 12).map(([s, pt]) => `<div><i style="background:${s.col}"></i>${esc(s.name)} <b>${fmt(pt[1], g.o.unit, g.o.decimals)}</b></div>`).join('') +
        ann.map((a) => `<div style="color:#ffe066;margin-top:3px">◆ ${esc(a.text)}</div>`).join('');
      tip.style.display = 'block';
      tip.style.left = Math.min(window.innerWidth - tip.offsetWidth - 8, e.clientX + 14) + 'px'; tip.style.top = (e.clientY + 14) + 'px';
      redrawCrosshair();
    });
    cv.addEventListener('mouseleave', () => { S.hoverT = null; tip.style.display = 'none'; redrawCrosshair(); });
  }
  let chRaf = 0;
  function redrawCrosshair() {
    if (chRaf) return;
    chRaf = requestAnimationFrame(() => { chRaf = 0;
      for (const id in S.panels) { const pp = S.panels[id]; if (pp.p.type === 'timeseries' && pp.data && pp.el.isConnected) { const cv = pp.el.querySelector('.bd canvas'); if (cv) drawTimeseries(cv, pp.data, pp.p, pp.range); } } });
  }

  // ── ダッシュボード ─────────────────────────────────
  async function loadList() {
    S.list = (await get('/api/nirvana/dashboards')).dashboards;
    $('#nbDash').innerHTML = S.list.map((d) => `<option value="${esc(d.uid)}">${esc(d.title)}</option>`).join('');
  }
  async function openDash(uid) {
    try { S.dash = await get('/api/nirvana/dashboards/' + encodeURIComponent(uid)); } catch (e) { toast('読み込めません: ' + e.message, true); return; }
    S.dirty = false; S.vars = lsGet('nbVars:' + uid, {}); S.hidden = {};
    S.range = { from: (S.dash.time || {}).from || 'now-1h', to: (S.dash.time || {}).to || 'now' };
    S.refresh = S.dash.refresh || '';
    $('#nbDash').value = uid; syncRangeUi();
    lsSet('nbDash', uid);
    try { const u = new URL(location.href); u.searchParams.set('view', 'board'); u.searchParams.set('d', uid); history.replaceState(null, '', u); } catch (e) { /* 無くても動く */ }
    await loadVarOptions();
    layout(); refreshAll(); armTimer();
  }
  function syncRangeUi() {
    const r = $('#nbRange'); r.value = RANGES.some(([v]) => v === S.range.from) && S.range.to === 'now' ? S.range.from : 'custom';
    if (r.value === 'custom' && ![...r.options].some((o) => o.value === 'custom')) r.value = 'now-1h';
    $('#nbRefresh').value = S.refresh;
  }
  function layout() {
    const grid = $('#nbGrid'); grid.innerHTML = ''; S.panels = {};
    const panels = (S.dash.panels || []).slice().sort((a, b) => a.gridPos.y - b.gridPos.y || a.gridPos.x - b.gridPos.x);
    if (!panels.length) grid.innerHTML = '<div class="empty" style="grid-column:1/-1;padding:40px;color:var(--dim)">パネルがありません。「＋ パネル」で追加してください</div>';
    for (const p of panels) {
      const el = document.createElement('div'); el.className = 'nbp'; el.dataset.id = p.id;
      place(el, p.gridPos);
      el.innerHTML = `<div class="hd"><span class="t">${esc(interp(p.title || ''))}</span><span class="st"></span><span class="tools">
        <span data-a="edit" title="編集">✎</span><span data-a="view" title="大きく表示">⤢</span><span data-a="dup" title="複製">⧉</span><span data-a="explore" title="EXPLORE で開く">⌕</span><span data-a="del" title="削除">✕</span></span></div>
        <div class="bd"></div><div class="lg"></div><i class="rs"></i>`;
      grid.appendChild(el);
      S.panels[p.id] = { p, el };
      el.querySelector('.tools').addEventListener('click', (e) => { const a = e.target.dataset.a; if (a) panelAction(a, p); });
      bindDrag(el, p);
    }
  }
  function place(el, g) { el.style.setProperty('--h', Math.max(2, g.h)); el.style.gridColumn = `${g.x + 1} / span ${Math.max(1, Math.min(12, g.w))}`; el.style.gridRow = `${g.y + 1} / span ${Math.max(1, g.h)}`; }
  async function refreshPanel(id) {
    const pp = S.panels[id]; if (!pp) return;
    const st = pp.el.querySelector('.st'); st.className = 'st'; st.textContent = '…';
    const range = rangeNow();
    try {
      pp.data = await fetchPanel(pp.p); pp.range = range; st.textContent = '';
      renderPanelBody(pp.el, pp.p, pp.data, range);
    } catch (e) { st.className = 'st err'; st.textContent = '⚠ ' + e.message.slice(0, 80); st.title = e.message; }
  }
  async function loadAnnotations() {
    const r = rangeNow();
    try { S.anns = (await get('/api/nirvana/annotations?' + qs({ start: r.start, end: r.end }))).annotations; } catch (e) { S.anns = []; }
  }
  async function refreshAll() {
    if (!S.dash) return;
    await loadAnnotations();
    await Promise.all(Object.keys(S.panels).map(refreshPanel));
  }
  function armTimer() {
    clearInterval(S.timer); S.timer = null;
    const sec = durSec(S.refresh);
    if (sec && S.view === 'board') S.timer = setInterval(() => { if (!document.hidden && S.view === 'board' && !$('#nbModal').classList.contains('show')) refreshAll(); }, sec * 1000);
  }
  function markDirty() { S.dirty = true; $('#nbSave').textContent = '保存 *'; }
  async function save() {
    if (!S.dash) return;
    S.dash.time = { ...S.range }; S.dash.refresh = S.refresh;
    try {
      S.dash = await req('PUT', '/api/nirvana/dashboards/' + encodeURIComponent(S.dash.uid), S.dash);
      S.dirty = false; $('#nbSave').textContent = '保存'; toast(`保存しました(version ${S.dash.version})`); await loadList(); $('#nbDash').value = S.dash.uid;
    } catch (e) { toast('保存できません: ' + e.message, true); }
  }
  function nextId() { return Math.max(0, ...(S.dash.panels || []).map((p) => +p.id || 0)) + 1; }
  function bottomY() { return Math.max(0, ...(S.dash.panels || []).map((p) => p.gridPos.y + p.gridPos.h)); }
  function panelAction(a, p) {
    if (a === 'edit') editPanel(p);
    else if (a === 'view') viewPanel(p);
    else if (a === 'dup') { const c = JSON.parse(JSON.stringify(p)); c.id = nextId(); c.gridPos.y = bottomY(); c.title = (c.title || '') + ' (複製)'; S.dash.panels.push(c); markDirty(); layout(); refreshAll(); }
    else if (a === 'del') { if (!confirm(`パネル「${p.title}」を削除しますか?`)) return; S.dash.panels = S.dash.panels.filter((x) => x !== p); markDirty(); layout(); refreshAll(); }
    else if (a === 'explore') { const t = (p.targets || [])[0] || {}; openExplore(p.ds || S.dsDefault, interp(t.expr || ''), p.type === 'logs' ? 'logs' : 'metrics'); }
  }
  function viewPanel(p) {
    modal(`<h3>${esc(interp(p.title || ''))}</h3><div class="nbp" style="height:min(70vh,560px)" id="vwP"><div class="hd"><span class="t"></span><span class="st"></span></div><div class="bd"></div><div class="lg"></div></div><div class="act"><button id="vwClose">閉じる</button></div>`, async (w) => {
      $('#vwClose', w).onclick = closeModal;
      const el = $('#vwP', w), range = rangeNow();
      try { const data = await fetchPanel(p); renderPanelBody(el, p, data, range); } catch (e) { el.querySelector('.st').textContent = '⚠ ' + e.message; }
    });
  }
  // ドラッグで移動・角でサイズ変更(編集モードのとき)
  function bindDrag(el, p) {
    const grid = $('#nbGrid');
    const start = (e, mode) => {
      if (!S.edit || e.button !== 0) return;
      if (mode === 'move' && e.target.closest('.tools')) return;
      e.preventDefault();
      const gr = grid.getBoundingClientRect(), colW = (gr.width + 10) / 12, rowH = 54;
      const sx = e.clientX, sy = e.clientY, g0 = { ...p.gridPos };
      const mv = (ev) => {
        const dx = Math.round((ev.clientX - sx) / colW), dy = Math.round((ev.clientY - sy) / rowH);
        if (mode === 'move') { p.gridPos.x = Math.max(0, Math.min(12 - g0.w, g0.x + dx)); p.gridPos.y = Math.max(0, g0.y + dy); }
        else { p.gridPos.w = Math.max(2, Math.min(12 - g0.x, g0.w + dx)); p.gridPos.h = Math.max(2, g0.h + dy); }
        place(el, p.gridPos);
      };
      const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up);
        if (JSON.stringify(g0) !== JSON.stringify(p.gridPos)) { markDirty(); const pp = S.panels[p.id]; if (pp && pp.data) renderPanelBody(el, p, pp.data, pp.range); } };
      window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up);
    };
    el.querySelector('.hd').addEventListener('pointerdown', (e) => start(e, 'move'));
    el.querySelector('.rs').addEventListener('pointerdown', (e) => start(e, 'size'));
  }

  // パネルの編集
  function dsOptions(sel, metricsOnly) {
    return S.ds.filter((d) => !metricsOnly || d.type !== 'loki' || true).map((d) => `<option value="${esc(d.uid)}" ${d.uid === sel ? 'selected' : ''} ${d.available ? '' : 'disabled'}>${esc(d.name)}${d.available ? '' : '(未設定)'}</option>`).join('');
  }
  function editPanel(p, isNew) {
    const w0 = JSON.parse(JSON.stringify(p));
    const o = w0.options = w0.options || {};
    w0.targets = w0.targets && w0.targets.length ? w0.targets : [{ expr: '', legend: '' }];
    const thrText = (o.thresholds || []).map((t) => `${t.value === null || t.value === undefined ? 'base' : t.value}=${t.color}`).join(', ');
    const mapText = Object.entries(o.mappings || {}).map(([k, v]) => `${k}=${v}`).join(', ');
    modal(`<h3>${isNew ? 'パネルを追加' : 'パネルを編集'}</h3>
      <div class="row">
        <label>種類<select id="peType">${TYPES.map(([v, n]) => `<option value="${v}" ${v === w0.type ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
        <label style="flex:1;min-width:200px">タイトル<input id="peTitle" value="${esc(w0.title || '')}"></label>
        <label>データソース<select id="peDs">${dsOptions(w0.ds || S.dsDefault)}</select></label>
        <label>幅(1-12)<input id="peW" type="number" min="1" max="12" value="${w0.gridPos.w}" style="width:60px"></label>
        <label>高さ<input id="peH" type="number" min="2" max="20" value="${w0.gridPos.h}" style="width:60px"></label>
      </div>
      <div id="peQs"></div><button id="peAddQ">＋ 問い合わせ</button>
      <div class="prev nbp" id="pePrev"><div class="hd"><span class="t">プレビュー</span><span class="st"></span></div><div class="bd"></div><div class="lg"></div></div>
      <div class="row">
        <label>単位<select id="peUnit">${UNITS.map(([v, n]) => `<option value="${v}" ${v === (o.unit || 'none') ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
        <label>小数<input id="peDec" type="number" min="0" max="6" value="${o.decimals ?? ''}" style="width:56px"></label>
        <label>最小<input id="peMin" value="${o.min ?? ''}" style="width:70px"></label>
        <label>最大<input id="peMax" value="${o.max ?? ''}" style="width:70px"></label>
        <label>集計(数値/メーター/棒)<select id="peRed">${['last', 'avg', 'max', 'min', 'sum'].map((v) => `<option ${v === (o.reduce || 'last') ? 'selected' : ''}>${v}</option>`).join('')}</select></label>
        <label>塗り<select id="peFill"><option value="1" ${o.fill !== false ? 'selected' : ''}>あり</option><option value="0" ${o.fill === false ? 'selected' : ''}>なし</option></select></label>
        <label>積み上げ<select id="peStack"><option value="0">なし</option><option value="1" ${o.stack ? 'selected' : ''}>あり</option></select></label>
      </div>
      <div class="row">
        <label style="flex:1;min-width:260px">しきい値(例: base=#5dffa8, 0.5=#ffab2e, 0.8=#ff3b4e)<input id="peThr" value="${esc(thrText)}"></label>
        <label style="flex:1;min-width:220px">値の置き換え(例: 0=正常, 1=注意, 2=異常)<input id="peMap" value="${esc(mapText)}"></label>
      </div>
      <label style="display:flex">テキスト(テキストパネル)<textarea id="peText">${esc(o.content || '')}</textarea></label>
      <div class="act"><button id="peCancel">やめる</button><button id="pePrevBtn">プレビュー ⟳</button><button id="peOk" class="pri">${isNew ? '追加' : '反映'}</button></div>`, (w) => {
      const drawQs = () => {
        $('#peQs', w).innerHTML = w0.targets.map((t, i) => `<div class="q"><div class="row"><label style="flex:1">問い合わせ ${String.fromCharCode(65 + i)}<textarea data-i="${i}" class="peExpr" spellcheck="false" list="exNames">${esc(t.expr || '')}</textarea></label></div>
          <div class="row"><label style="flex:1">凡例(例: {{host_a}} ⇄ {{host_b}})<input data-i="${i}" class="peLeg" value="${esc(t.legend || '')}"></label><button data-i="${i}" class="peDelQ warn">削除</button></div></div>`).join('');
        $$('.peExpr', w).forEach((x) => { x.oninput = () => { w0.targets[+x.dataset.i].expr = x.value; }; x.onkeydown = (e) => { if (e.key === 'Enter' && e.shiftKey) { e.preventDefault(); prev(); } }; });
        $$('.peLeg', w).forEach((x) => { x.oninput = () => { w0.targets[+x.dataset.i].legend = x.value; }; });
        $$('.peDelQ', w).forEach((x) => { x.onclick = () => { w0.targets.splice(+x.dataset.i, 1); if (!w0.targets.length) w0.targets.push({ expr: '', legend: '' }); drawQs(); }; });
      };
      const collect = () => {
        w0.type = $('#peType', w).value; w0.title = $('#peTitle', w).value; w0.ds = $('#peDs', w).value;
        w0.gridPos.w = Math.max(1, Math.min(12, +$('#peW', w).value || 6)); w0.gridPos.h = Math.max(2, +$('#peH', w).value || 4);
        o.unit = $('#peUnit', w).value; o.decimals = $('#peDec', w).value === '' ? undefined : +$('#peDec', w).value;
        o.min = $('#peMin', w).value === '' ? undefined : +$('#peMin', w).value; o.max = $('#peMax', w).value === '' ? undefined : +$('#peMax', w).value;
        o.reduce = $('#peRed', w).value; o.fill = $('#peFill', w).value === '1'; o.stack = $('#peStack', w).value === '1'; o.content = $('#peText', w).value;
        o.thresholds = $('#peThr', w).value.split(',').map((x) => x.trim()).filter(Boolean).map((x) => { const [k, c] = x.split('='); return { value: k.trim() === 'base' ? null : +k, color: (c || '#33d6ff').trim() }; });
        const mp = {}; $('#peMap', w).value.split(',').map((x) => x.trim()).filter(Boolean).forEach((x) => { const [k, v] = x.split('='); if (v !== undefined) mp[k.trim()] = v.trim(); });
        o.mappings = Object.keys(mp).length ? mp : undefined;
        return w0;
      };
      const prev = async () => {
        collect(); const el = $('#pePrev', w), stEl = el.querySelector('.st'); stEl.textContent = '…';
        try { const r = rangeNow(); const d = await fetchPanel(w0); renderPanelBody(el, w0, d, r); stEl.textContent = ''; } catch (e) { stEl.className = 'st err'; stEl.textContent = '⚠ ' + e.message; }
      };
      drawQs();
      $('#peAddQ', w).onclick = () => { w0.targets.push({ expr: '', legend: '' }); drawQs(); };
      $('#peCancel', w).onclick = closeModal; $('#pePrevBtn', w).onclick = prev;
      $('#peType', w).onchange = prev;
      $('#peOk', w).onclick = () => {
        collect();
        if (isNew) S.dash.panels.push(w0); else Object.assign(p, w0);
        closeModal(); markDirty(); layout(); refreshAll();
      };
      prev();
    });
  }
  function addPanel(preset) {
    const p = preset || { id: nextId(), type: 'timeseries', title: '新しいパネル', ds: S.dsDefault, gridPos: { x: 0, y: bottomY(), w: 6, h: 6 }, targets: [{ expr: '', legend: '' }], options: {} };
    p.id = nextId(); p.gridPos = p.gridPos || { x: 0, y: bottomY(), w: 6, h: 6 };
    editPanel(p, true);
  }
  async function addAnnotation(t) {
    const text = prompt(`注釈(${new Date((t || Date.now() / 1000) * 1000).toLocaleString()})`);
    if (!text) return;
    try { await req('POST', '/api/nirvana/annotations', { time: t || Date.now() / 1000, text, tags: ['user'], dashboard: S.dash && S.dash.uid }); toast('注釈を付けました'); refreshAll(); }
    catch (e) { toast(e.message, true); }
  }
  function moreMenu() {
    modal(`<h3>ダッシュボード</h3>
      <div class="row"><button id="mmNew">新規作成</button><button id="mmDup">複製</button><button id="mmSet">設定(名前・タグ・変数)</button>
      <button id="mmExp">JSON を書き出す</button><button id="mmImp">JSON を取り込む</button><button id="mmDel" class="warn">削除</button></div>
      <div class="sec">一覧</div><div>${S.list.map((d) => `<div class="ar" style="grid-template-columns:1fr auto auto"><a href="#" data-u="${esc(d.uid)}" style="color:#8fe9ff">${esc(d.title)}</a><span class="x">${(d.tags || []).map(esc).join(', ')}</span><span class="x">パネル ${d.panels} ・ v${d.version}</span></div>`).join('')}</div>
      <div class="act"><button id="mmClose">閉じる</button></div>`, (w) => {
      $('#mmClose', w).onclick = closeModal;
      $$('a[data-u]', w).forEach((a) => { a.onclick = (e) => { e.preventDefault(); closeModal(); openDash(a.dataset.u); }; });
      $('#mmNew', w).onclick = async () => { const t = prompt('ダッシュボードの名前'); if (!t) return;
        try { const d = await req('POST', '/api/nirvana/dashboards', { title: t, panels: [], variables: [], time: { from: 'now-1h', to: 'now' }, refresh: '30s', tags: [] }); closeModal(); await loadList(); openDash(d.uid); } catch (e) { toast(e.message, true); } };
      $('#mmDup', w).onclick = async () => { const c = JSON.parse(JSON.stringify(S.dash)); delete c.uid; delete c.version; c.title += ' (複製)';
        try { const d = await req('POST', '/api/nirvana/dashboards', c); closeModal(); await loadList(); openDash(d.uid); } catch (e) { toast(e.message, true); } };
      $('#mmDel', w).onclick = async () => { if (!confirm(`「${S.dash.title}」を削除しますか?`)) return;
        await req('DELETE', '/api/nirvana/dashboards/' + encodeURIComponent(S.dash.uid)); closeModal(); await loadList(); if (S.list[0]) openDash(S.list[0].uid); };
      $('#mmExp', w).onclick = () => { const b = new Blob([JSON.stringify(S.dash, null, 2)], { type: 'application/json' }); const a = document.createElement('a');
        a.href = URL.createObjectURL(b); a.download = (S.dash.title || 'dashboard').replace(/[\\/:*?"<>|]/g, '_') + '.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); };
      $('#mmImp', w).onclick = importDialog; $('#mmSet', w).onclick = settingsDialog;
    });
  }
  function importDialog() {
    modal(`<h3>JSON を取り込む</h3><div style="color:var(--dim);margin-bottom:6px">ニルヴァーナで書き出した JSON のほか、Grafana の書き出し(panels / templating / targets[].expr)もそのまま読めます</div>
      <input type="file" id="imFile" accept="application/json,.json"><textarea id="imTxt" style="min-height:220px" placeholder="ここに JSON を貼り付け"></textarea>
      <label style="flex-direction:row;align-items:center;gap:6px"><input type="checkbox" id="imOver"> 同じ uid があれば上書き</label>
      <div class="act"><button id="imCancel">やめる</button><button id="imOk" class="pri">取り込む</button></div>`, (w) => {
      $('#imCancel', w).onclick = closeModal;
      $('#imFile', w).onchange = async (e) => { const f = e.target.files[0]; if (f) $('#imTxt', w).value = await f.text(); };
      $('#imOk', w).onclick = async () => {
        let d; try { d = JSON.parse($('#imTxt', w).value); } catch (e) { toast('JSON が読めません: ' + e.message, true); return; }
        try { const r = await req('POST', '/api/nirvana/dashboards/import?overwrite=' + ($('#imOver', w).checked ? 'true' : 'false'), d); closeModal(); await loadList(); openDash(r.uid); toast('取り込みました'); }
        catch (e) { toast(e.message, true); }
      };
    });
  }
  function settingsDialog() {
    const vars = JSON.parse(JSON.stringify(S.dash.variables || []));
    modal(`<h3>ダッシュボードの設定</h3>
      <div class="row"><label style="flex:1">名前<input id="sdT" value="${esc(S.dash.title)}"></label><label style="flex:1">タグ(カンマ区切り)<input id="sdTags" value="${esc((S.dash.tags || []).join(', '))}"></label></div>
      <div class="sec">変数</div><div style="color:var(--dim)">query: label_values(メトリクス, ラベル) ・ custom: a,b,c ・ 問い合わせでは $名前 で使う($__interval / $__rate_interval / $__range も使えます)</div>
      <div id="sdVars"></div><button id="sdAdd">＋ 変数</button>
      <div class="act"><button id="sdCancel">やめる</button><button id="sdOk" class="pri">反映</button></div>`, (w) => {
      const draw = () => { $('#sdVars', w).innerHTML = vars.map((v, i) => `<div class="q row">
        <label>名前<input data-i="${i}" data-k="name" value="${esc(v.name || '')}" style="width:90px"></label>
        <label>表示名<input data-i="${i}" data-k="label" value="${esc(v.label || '')}" style="width:90px"></label>
        <label>種類<select data-i="${i}" data-k="type"><option value="query" ${v.type !== 'custom' ? 'selected' : ''}>query</option><option value="custom" ${v.type === 'custom' ? 'selected' : ''}>custom</option></select></label>
        <label>データソース<select data-i="${i}" data-k="ds">${dsOptions(v.ds || S.dsDefault)}</select></label>
        <label style="flex:1;min-width:220px">中身<input data-i="${i}" data-k="query" value="${esc(v.query || '')}"></label>
        <label>複数<input type="checkbox" data-i="${i}" data-k="multi" ${v.multi ? 'checked' : ''}></label>
        <label>すべて<input type="checkbox" data-i="${i}" data-k="includeAll" ${v.includeAll ? 'checked' : ''}></label>
        <button data-del="${i}" class="warn">削除</button></div>`).join('');
        $$('[data-k]', w).forEach((x) => { x.onchange = () => { vars[+x.dataset.i][x.dataset.k] = x.type === 'checkbox' ? x.checked : x.value; }; });
        $$('[data-del]', w).forEach((x) => { x.onclick = () => { vars.splice(+x.dataset.del, 1); draw(); }; }); };
      draw();
      $('#sdAdd', w).onclick = () => { vars.push({ name: 'var' + (vars.length + 1), type: 'query', ds: S.dsDefault, query: '', multi: false, includeAll: true }); draw(); };
      $('#sdCancel', w).onclick = closeModal;
      $('#sdOk', w).onclick = async () => { S.dash.title = $('#sdT', w).value; S.dash.tags = $('#sdTags', w).value.split(',').map((x) => x.trim()).filter(Boolean);
        S.dash.variables = vars; closeModal(); markDirty(); await loadVarOptions(); layout(); refreshAll(); };
    });
  }

  // ツールバー
  $('#nbDash').onchange = (e) => { if (S.dirty && !confirm('保存していない変更があります。切り替えますか?')) { e.target.value = S.dash.uid; return; } openDash(e.target.value); };
  $('#nbRange').onchange = (e) => {
    if (e.target.value === 'custom') {
      const a = prompt('開始(例: 2026-11-16 09:00)'); const b = prompt('終了(空なら今)');
      const ta = Date.parse(a), tb = b ? Date.parse(b) : Date.now();
      if (!a || Number.isNaN(ta) || Number.isNaN(tb) || tb <= ta) { toast('期間が読めません', true); syncRangeUi(); return; }
      S.range = { from: String(ta / 1000), to: String(tb / 1000) };
    } else S.range = { from: e.target.value, to: 'now' };
    markDirty(); refreshAll();
  };
  $('#nbRefresh').onchange = (e) => { S.refresh = e.target.value; markDirty(); armTimer(); };
  $('#nbReload').onclick = refreshAll;
  $('#nbSave').onclick = save;
  $('#nbEdit').onclick = () => { S.edit = !S.edit; root.classList.toggle('editing', S.edit); $('#nbEdit').classList.toggle('on', S.edit); };
  $('#nbAddPanel').onclick = () => addPanel();
  $('#nbAddAnn').onclick = () => addAnnotation(Date.now() / 1000);
  $('#nbMore').onclick = moreMenu;
  window.addEventListener('resize', () => { if (S.view === 'board') for (const id in S.panels) { const pp = S.panels[id]; if (pp.data) renderPanelBody(pp.el, pp.p, pp.data, pp.range); } });
  window.addEventListener('beforeunload', (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ''; } });

  // ── EXPLORE ───────────────────────────────────
  const EX = { hist: lsGet('nbExHist', []) };
  function exDsChanged() {
    const ds = $('#exDs').value;
    if (ds === 'loki') $('#exKind').value = /^\s*\{/.test($('#exExpr').value) || !$('#exExpr').value ? 'logs' : $('#exKind').value;
    get('/api/nirvana/metric-names?ds=' + ds).then((d) => { $('#exNames').innerHTML = (d.names || []).map((n) => `<option value="${esc(n)}">`).join(''); }).catch(() => {});
  }
  function drawHist() { $('#exHist').innerHTML = EX.hist.slice(0, 12).map((h, i) => `<span data-i="${i}" title="${esc(h.ds)}">${esc(h.expr.slice(0, 60))}</span>`).join('');
    $$('#exHist span').forEach((sp) => { sp.onclick = () => { const h = EX.hist[+sp.dataset.i]; $('#exDs').value = h.ds; $('#exKind').value = h.kind; $('#exExpr').value = h.expr; runExplore(); }; }); }
  async function runExplore() {
    const ds = $('#exDs').value, kind = $('#exKind').value, expr = $('#exExpr').value.trim();
    if (!expr && kind === 'metrics') { toast('問い合わせを入力してください', true); return; }
    EX.hist = [{ ds, kind, expr }, ...EX.hist.filter((h) => !(h.expr === expr && h.ds === ds))].slice(0, 30); lsSet('nbExHist', EX.hist); drawHist();
    const res = $('#exRes'); res.innerHTML = '<div class="var">問い合わせ中…</div>';
    const saved = S.range; S.range = { from: $('#exRange').value, to: 'now' }; const range = rangeNow(); S.range = saved;
    const p = { id: 'ex', type: kind === 'logs' ? 'logs' : 'timeseries', title: expr, ds, targets: [{ expr, legend: '' }], options: { fill: true } };
    try {
      let data;
      if (kind === 'logs') data = { series: [], logs: (await get('/api/nirvana/logs?' + qs({ ds, expr, start: range.start, end: range.end, limit: 500 }))).logs };
      else { const d = await get('/api/nirvana/query?' + qs({ ds, expr, start: range.start, end: range.end, step: range.step }));
        data = { series: d.series.map((s) => ({ name: legendOf('', s.labels), labels: s.labels, points: s.points })), logs: [] }; }
      res.innerHTML = '';
      const mk = (pp, h) => { const el = document.createElement('div'); el.className = 'nbp'; el.style.height = h + 'px';
        el.innerHTML = `<div class="hd"><span class="t">${esc(pp.type === 'table' ? '表' : pp.type === 'logs' ? `ログ ${(data.logs || []).length} 件` : `時系列 ${data.series.length} 系列`)}</span><span class="st"></span></div><div class="bd"></div><div class="lg"></div>`;
        res.appendChild(el); renderPanelBody(el, pp, data, range); };
      if (kind === 'logs') mk(p, 520);
      else { S.anns = []; mk(p, 360); mk({ ...p, type: 'table' }, Math.min(420, 60 + data.series.length * 22)); }
      EX.last = p;
    } catch (e) { res.innerHTML = `<div class="nbp" style="padding:12px;color:#ffb0b8">⚠ ${esc(e.message)}</div>`; }
  }
  function openExplore(ds, expr, kind) { B.setView('explore'); $('#exDs').value = ds; $('#exExpr').value = expr; $('#exKind').value = kind || 'metrics'; exDsChanged(); runExplore(); }
  $('#exRun').onclick = runExplore;
  $('#exExpr').addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.shiftKey) { e.preventDefault(); runExplore(); } });
  $('#exExpr').setAttribute('list', 'exNames');
  $('#exDs').onchange = exDsChanged;
  $('#exToPanel').onclick = async () => {
    const expr = $('#exExpr').value.trim(); if (!expr) return;
    if (!S.dash) { await loadList(); if (S.list[0]) S.dash = await get('/api/nirvana/dashboards/' + S.list[0].uid); }
    if (!S.dash) { toast('ダッシュボードがありません', true); return; }
    B.setView('board');
    addPanel({ type: $('#exKind').value === 'logs' ? 'logs' : 'timeseries', title: expr.slice(0, 50), ds: $('#exDs').value, targets: [{ expr, legend: '' }], options: {}, gridPos: { x: 0, y: bottomY(), w: 6, h: 6 } });
  };

  // ── ALERTS ────────────────────────────────────
  let alTimer = null;
  async function renderAlerts() {
    let rules, state, cps;
    try { [rules, state, cps] = await Promise.all([get('/api/nirvana/alert-rules'), get('/api/nirvana/alert-state'), get('/api/nirvana/contact-points')]); }
    catch (e) { $('#alPane').innerHTML = `<div style="color:#ffb0b8">⚠ ${esc(e.message)}</div>`; return; }
    const inst = state.instances, errs = state.errors || {};
    const byRule = {}; for (const i of inst) (byRule[i.rule] = byRule[i.rule] || []).push(i);
    const firingN = inst.filter((i) => i.state === 'firing').length, pendN = inst.filter((i) => i.state === 'pending').length;
    $('#alSum').innerHTML = `ルール <b>${rules.rules.length}</b> ・ 発報中 <b style="color:#ff6b7a">${firingN}</b> ・ 保留中 <b style="color:#ffab2e">${pendN}</b>`;
    const cpName = Object.fromEntries(cps.contact_points.map((c) => [c.uid, c.name]));
    $('#alPane').innerHTML = `<div class="sec">アラートルール</div>${rules.rules.length ? '' : '<div class="var">まだありません。「＋ ルール」で作れます</div>'}
      ${rules.rules.map((r) => { const is = byRule[r.uid] || []; const worst = !r.enabled && r.enabled !== undefined ? 'off' : errs[r.uid] ? 'error' : is.some((i) => i.state === 'firing') ? 'firing' : is.some((i) => i.state === 'pending') ? 'pending' : 'normal';
        return `<div class="ar"><span class="badge b-${worst}">${{ off: 'OFF', error: 'ERROR', firing: 'FIRING', pending: 'PENDING', normal: 'NORMAL' }[worst]}</span>
        <span><b style="color:#fff">${esc(r.title)}</b><div class="x">${esc(r.severity || 'warning')} ・ ${esc(r.ds || 'internal')} ・ ${esc(r.reducer || 'last')} ${esc(r.op || '>')} ${esc(r.threshold)} ・ ${esc(r.for || '0s')} 継続 ・ 通知 ${(r.contacts || []).map((c) => esc(cpName[c] || c)).join(', ') || 'なし'}</div></span>
        <span class="x">${esc(r.expr)}${errs[r.uid] ? `<div style="color:#ff6b7a">${esc(errs[r.uid])}</div>` : ''}</span>
        <span class="x">${is.filter((i) => i.state !== 'normal').slice(0, 4).map((i) => `<div><span class="badge b-${i.state}" style="min-width:0">${i.state}</span> ${esc(i.key)} = ${fmt(i.value)}</div>`).join('') || `系列 ${is.length}`}</span>
        <span><button data-e="${esc(r.uid)}">編集</button> <button data-d="${esc(r.uid)}" class="warn">削除</button></span></div>`; }).join('')}
      <div class="sec">連絡先(webhook / メール)</div>${cps.contact_points.length ? '' : '<div class="var">まだありません。「＋ 連絡先」で作れます</div>'}
      ${cps.contact_points.map((c) => `<div class="ar" style="grid-template-columns:1fr 2fr auto"><b style="color:#fff">${esc(c.name)}</b><span class="x">${c.type === 'email' ? '✉ ' + esc(c.to) + ' (' + esc(c.smtp_host) + ')' : esc(c.url)}</span><span><button data-t="${esc(c.uid)}">試験送信</button> <button data-cd="${esc(c.uid)}" class="warn">削除</button></span></div>`).join('')}
      <div class="sec">状態の履歴</div>${state.history.slice().reverse().slice(0, 60).map((h) => `<div class="ar" style="grid-template-columns:150px 1fr 1fr 100px"><span class="x">${new Date(h.t * 1000).toLocaleString()}</span><span>${esc(h.title)}</span><span class="x">${esc(h.key)}</span><span><span class="badge b-${h.to}" style="min-width:0">${esc(h.from)}→${esc(h.to)}</span></span></div>`).join('') || '<div class="var">まだありません</div>'}`;
    $$('[data-e]', $('#alPane')).forEach((b) => { b.onclick = () => ruleDialog(rules.rules.find((r) => r.uid === b.dataset.e), cps.contact_points); });
    $$('[data-d]', $('#alPane')).forEach((b) => { b.onclick = async () => { if (!confirm('このルールを削除しますか?')) return; await req('DELETE', '/api/nirvana/alert-rules/' + b.dataset.d); renderAlerts(); }; });
    $$('[data-t]', $('#alPane')).forEach((b) => { b.onclick = async () => { try { const r = await req('POST', `/api/nirvana/contact-points/${b.dataset.t}/test`); toast(`送りました(HTTP ${r.status})`); } catch (e) { toast(e.message, true); } }; });
    $$('[data-cd]', $('#alPane')).forEach((b) => { b.onclick = async () => { if (!confirm('この連絡先を削除しますか?')) return; await req('DELETE', '/api/nirvana/contact-points/' + b.dataset.cd); renderAlerts(); }; });
    $('#alNew').onclick = () => ruleDialog(null, cps.contact_points);
  }
  function ruleDialog(r, cps) {
    const x = r ? { ...r } : { title: '', ds: S.dsDefault, expr: 'netlab_link_util', reducer: 'last', op: '>', threshold: 0.8, window: '5m', for: '1m', severity: 'warning', summary: '', contacts: [], enabled: true };
    modal(`<h3>${r ? 'ルールを編集' : 'ルールを作る'}</h3>
      <div class="row"><label style="flex:1">名前<input id="rT" value="${esc(x.title)}"></label><label>データソース<select id="rDs">${dsOptions(x.ds)}</select></label>
        <label>重要度<select id="rSev">${['critical', 'warning', 'info'].map((v) => `<option ${v === x.severity ? 'selected' : ''}>${v}</option>`).join('')}</select></label>
        <label>有効<input type="checkbox" id="rEn" ${x.enabled !== false ? 'checked' : ''}></label></div>
      <label style="display:flex">問い合わせ(系列ごとに判定します)<textarea id="rExpr" spellcheck="false" list="exNames">${esc(x.expr)}</textarea></label>
      <div class="row"><label>期間<select id="rWin">${['1m', '5m', '15m', '1h'].map((v) => `<option ${v === x.window ? 'selected' : ''}>${v}</option>`).join('')}</select></label>
        <label>集計<select id="rRed">${['last', 'avg', 'max', 'min', 'sum', 'count'].map((v) => `<option ${v === x.reducer ? 'selected' : ''}>${v}</option>`).join('')}</select></label>
        <label>条件<select id="rOp">${['>', '<', '>=', '<=', '==', '!='].map((v) => `<option ${v === x.op ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></label>
        <label>しきい値<input id="rThr" value="${esc(x.threshold)}" style="width:90px"></label>
        <label>継続(この間ずっと満たしたら発報)<select id="rFor">${['0s', '30s', '1m', '2m', '5m', '10m'].map((v) => `<option ${v === x.for ? 'selected' : ''}>${v}</option>`).join('')}</select></label></div>
      <label style="display:flex">要約(通知文)<input id="rSum" value="${esc(x.summary || '')}"></label>
      <div class="sec">通知先</div><div>${cps.length ? cps.map((c) => `<label style="flex-direction:row;gap:6px;align-items:center"><input type="checkbox" class="rCp" value="${esc(c.uid)}" ${(x.contacts || []).includes(c.uid) ? 'checked' : ''}>${esc(c.name)}</label>`).join('') : '<span class="var">連絡先はまだありません</span>'}</div>
      <div class="sec">今の値で試す</div><div id="rPrev" class="x"></div>
      <div class="act"><button id="rCancel">やめる</button><button id="rTry">試す</button><button id="rOk" class="pri">保存</button></div>`, (w) => {
      const collect = () => ({ ...x, title: $('#rT', w).value, ds: $('#rDs', w).value, severity: $('#rSev', w).value, enabled: $('#rEn', w).checked,
        expr: $('#rExpr', w).value, window: $('#rWin', w).value, reducer: $('#rRed', w).value, op: $('#rOp', w).value, threshold: +$('#rThr', w).value,
        for: $('#rFor', w).value, summary: $('#rSum', w).value, contacts: $$('.rCp', w).filter((c) => c.checked).map((c) => c.value) });
      $('#rCancel', w).onclick = closeModal;
      $('#rTry', w).onclick = async () => { try { const d = await req('POST', '/api/nirvana/alert-rules/preview', { ...collect(), title: collect().title || '試験' });
        $('#rPrev', w).innerHTML = d.results.length ? d.results.map((q) => `<div><span class="badge b-${q.firing ? 'firing' : 'normal'}" style="min-width:0">${q.firing ? '条件を満たす' : '満たさない'}</span> ${esc(legendOf('', q.labels))} = ${fmt(q.value)}</div>`).join('') : '系列がありません';
      } catch (e) { $('#rPrev', w).innerHTML = `<span style="color:#ff6b7a">⚠ ${esc(e.message)}</span>`; } };
      $('#rOk', w).onclick = async () => { const b = collect();
        try { if (r) await req('PUT', '/api/nirvana/alert-rules/' + r.uid, b); else await req('POST', '/api/nirvana/alert-rules', b); closeModal(); toast('保存しました'); renderAlerts(); }
        catch (e) { toast(e.message, true); } };
    });
  }
  $('#alNewCp').onclick = () => {
    modal(`<h3>連絡先</h3><div class="row"><label>名前<input id="cpN"></label><label>種類<select id="cpT"><option value="webhook">webhook</option><option value="email">メール</option></select></label></div>
      <div class="row" id="cpW"><label style="flex:1">URL<input id="cpU" placeholder="https://hooks.example/..."></label></div>
      <div class="row" id="cpE" style="display:none"><label style="flex:1">宛先(カンマ区切り)<input id="cpTo" placeholder="noc@example.com"></label><label>SMTP サーバー<input id="cpH" placeholder="smtp.example.com"></label><label>ポート<input id="cpP" value="25" style="width:5em"></label>
        <label>差出人<input id="cpF" placeholder="nirvana@example.com"></label><label>ユーザー<input id="cpUs" placeholder="(認証する場合)"></label><label style="flex-direction:row;gap:6px;align-items:center"><input type="checkbox" id="cpS">STARTTLS</label></div>
      <div style="color:var(--dim)">発報・解消のたびに、webhook は Grafana / Alertmanager に近い JSON を POST、メールは件名に状態とルール名を入れて送ります。SMTP のパスワードは保存せず、サーバーの環境変数 NIRVANA_SMTP_PASSWORD から読みます</div>
      <div class="act"><button id="cpC">やめる</button><button id="cpOk" class="pri">保存</button></div>`, (w) => {
      $('#cpC', w).onclick = closeModal;
      $('#cpT', w).onchange = () => { const em = $('#cpT', w).value === 'email'; $('#cpW', w).style.display = em ? 'none' : ''; $('#cpE', w).style.display = em ? '' : 'none'; };
      $('#cpOk', w).onclick = async () => {
        const em = $('#cpT', w).value === 'email';
        const body = em ? { name: $('#cpN', w).value, type: 'email', to: $('#cpTo', w).value, smtp_host: $('#cpH', w).value, smtp_port: $('#cpP', w).value, from: $('#cpF', w).value, user: $('#cpUs', w).value, starttls: $('#cpS', w).checked }
          : { name: $('#cpN', w).value, url: $('#cpU', w).value, type: 'webhook' };
        try { await req('POST', '/api/nirvana/contact-points', body); closeModal(); renderAlerts(); } catch (e) { toast(e.message, true); }
      };
    });
  };

  // ── 表示の切り替え ─────────────────────────────────
  let inited = false;
  async function init() {
    if (inited) return; inited = true;
    try { const d = await get('/api/nirvana/datasources'); S.ds = d.datasources; S.dsDefault = d.default; }
    catch (e) { S.available = false; return; }
    const opts = dsOptions(S.dsDefault); $('#exDs').innerHTML = opts;
    exDsChanged(); drawHist();
    await loadList();
    const want = new URLSearchParams(location.search).get('d') || lsGet('nbDash', null);
    const uid = S.list.some((d) => d.uid === want) ? want : (S.list[0] && S.list[0].uid);
    if (uid) await openDash(uid);
  }
  window.NirvanaBoard = {
    async show(view) {
      S.view = view;
      $('#nbBoard').style.display = view === 'board' ? 'flex' : 'none';
      $('#nbExplore').style.display = view === 'explore' ? 'flex' : 'none';
      $('#nbAlerts').style.display = view === 'alerts' ? 'flex' : 'none';
      try { const u = new URL(location.href); u.searchParams.set('view', view); history.replaceState(null, '', u); } catch (e) { /* 無くても動く */ }
      await init();
      clearInterval(alTimer); alTimer = null;
      if (view === 'board') { armTimer(); if (S.dash) refreshAll(); }
      if (view === 'alerts') { renderAlerts(); alTimer = setInterval(() => { if (!document.hidden && S.view === 'alerts' && !$('#nbModal').classList.contains('show')) renderAlerts(); }, 10000); }
    },
    hide() { S.view = null; clearInterval(S.timer); clearInterval(alTimer); tip.style.display = 'none'; closeModal(); },
    state: () => ({ view: S.view, dash: S.dash && S.dash.uid, panels: Object.keys(S.panels).length, dirty: S.dirty, edit: S.edit,
      rendered: Object.values(S.panels).filter((p) => p.data).length, errors: $$('.nbp .st.err').map((e) => e.textContent) }),
  };
  // ボード機能の無いサーバーではボタンを隠す
  fetch('/api/nirvana/datasources', { headers: B.token() ? { 'X-Session-Token': B.token() } : {} })
    .then((r) => { if (r.status === 404) ['mBoard', 'mExplore', 'mAlerts'].forEach((id) => { const b = document.getElementById(id); if (b) b.style.display = 'none'; }); })
    .catch(() => {});
})();
