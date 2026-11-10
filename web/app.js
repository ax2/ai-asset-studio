"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ESC[c]);

const S = {
  cfg: {}, styles: [], pricing: null, stats: null, assets: [],
  view: "gallery", styleId: "", assetId: null, drawerAsset: null,
  modalCb: null, versions: [],
};

const VIEW_META = {
  gallery: ["资产画廊", "查看、筛选、复现与管理所有美术资产"],
  styles: ["风格配方", "编辑分块提示词、变量槽位与负面词"],
  batch: ["批量生成", "先 Dry run 验证配方，再切换真实生成"],
  export: ["导出与同步", "导出素材包、同步精选资产、检查元数据"],
  cost: ["成本看板", "额度、用量与参考单价"],
  settings: ["连接设置", "会话密钥、代理与端点自检"],
  help: ["使用说明", "零依赖本地运行，密钥不写入磁盘"],
};

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  let data = {};
  try { data = await res.json(); } catch (_) { /* non-JSON response */ }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

let toastTimer = null;
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast " + kind; }, 3000);
}

function confirmModal(title, body, cb) {
  $("#mTitle").textContent = title;
  $("#mBody").innerHTML = body;
  S.modalCb = cb;
  $("#modal").classList.remove("hidden");
}

function currentStyle() {
  return S.styles.find((s) => s.id === S.styleId) || S.styles[0] || { blocks: [], slots: {} };
}

function priceOf(model, quality) {
  const table = (S.pricing && S.pricing.image) || {};
  const row = table[model] || table["gpt-image-1.5"] || { low: 0.009 };
  return Number(row[quality] != null ? row[quality] : row.low);
}

function estimateOf(model, quality, count, batch) {
  return (priceOf(model, quality) * (batch ? 0.5 : 1) * Math.max(0, Number(count) || 0)).toFixed(4);
}

function buildPrompt(style, overrides, slots, extra) {
  const ov = overrides || {};
  const sl = slots || {};
  const parts = [];
  for (const b of style.blocks || []) {
    const raw = ov[b.key] !== undefined ? ov[b.key] : (b.en || "");
    const t = String(raw || "").trim().replace(/\.+$/, "");
    if (t) parts.push(t);
  }
  const slotLines = [];
  for (const key of ["subject", "asset_kind", "mood", "concept"]) {
    const v = String(sl[key] || "").trim().replace(/\.+$/, "");
    if (v) slotLines.push(key + ": " + v);
  }
  if (slotLines.length) parts.push(slotLines.join("; "));
  const ex = String(extra || "").trim().replace(/\.+$/, "");
  if (ex) parts.push(ex);
  let prompt = parts.join(", ");
  if (style.negative) prompt += ". Avoid: " + style.negative;
  return prompt;
}

function firstSlots(style) {
  const out = {};
  for (const key of ["subject", "asset_kind", "mood", "concept"]) {
    const values = (style.slots || {})[key] || [];
    if (values.length) out[key] = values[0];
  }
  return out;
}

function renderEval(container, style, prompt) {
  const rules = style.eval_rules || [];
  const low = String(prompt || "").toLowerCase();
  const hits = rules.filter((r) => low.includes(String(r.needle || "").toLowerCase()));
  const pct = rules.length ? Math.round(100 * hits.length / rules.length) : 100;
  container.innerHTML =
    `<div class="eval-bar"><div style="width:${pct}%"></div></div>` +
    `<div class="muted small">配方完整度 ${hits.length}/${rules.length}（${pct}%）</div>` +
    rules.map((r) => {
      const ok = low.includes(String(r.needle || "").toLowerCase());
      return `<div class="eval-row"><span class="eval-mark ${ok ? "" : "miss"}">${ok ? "✓" : "×"}</span>${esc(r.zh || r.key)}</div>`;
    }).join("");
}

function fillStyleSelects() {
  const opts = S.styles.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}${s.status === "queued" ? "（排队中）" : ""}</option>`).join("");
  $("#fStyle").innerHTML = `<option value="">全部风格</option>` + opts;
  $("#bStyle").innerHTML = opts;
  $("#eSyncStyle").innerHTML = `<option value="">全部风格</option>` + opts;
  if (S.styleId) {
    $("#bStyle").value = S.styleId;
  }
}

function renderStatus() {
  const keyOk = !!S.cfg.api_key_set;
  $("#dotKey").className = "dot " + (keyOk ? "ok" : "warn");
  $("#txtKey").textContent = keyOk
    ? (S.cfg.api_key_source === "session" ? "会话密钥（仅内存）" : "环境变量密钥")
    : "未设密钥（仅 Dry run）";
  const diskOk = !!S.cfg.asset_root_writable;
  $("#dotDisk").className = "dot " + (diskOk ? "ok" : "bad");
  $("#txtDisk").textContent = diskOk ? "资产盘可写" : "资产盘不可写";
  const used = S.stats ? S.stats.real_cost_usd : 0;
  $("#budgetChip").textContent = `本月 $${used} / $${S.cfg.monthly_budget_usd}`;
}

const KEY_SOURCE_LABEL = {
  session: "会话密钥（仅内存，重启失效）",
  env: "环境变量 OPENAI_API_KEY",
  none: "未设置",
};

function renderSettings() {
  const src = S.cfg.api_key_source || "none";
  $("#keyState").innerHTML =
    `<div class="kv"><span>当前密钥</span><span>${S.cfg.api_key_set ? esc(KEY_SOURCE_LABEL[src] || src) : "未设置"}</span></div>` +
    `<div class="kv"><span>代理 api_proxy</span><span>${esc(S.cfg.api_proxy || "(系统默认)")}</span></div>` +
    `<div class="kv"><span>API base</span><span>${esc(S.cfg.api_base || "https://api.openai.com/v1")}</span></div>`;
  $("#setProxy").value = S.cfg.api_proxy || "";
  $("#setBase").value = S.cfg.api_base || "";
  $("#settingsInfo").innerHTML =
    `<div class="kv"><span>监听地址</span><span>${esc(S.cfg.host)}:${esc(S.cfg.port)}</span></div>` +
    `<div class="kv"><span>默认模型</span><span>${esc(S.cfg.default_model)} / ${esc(S.cfg.default_quality)}</span></div>` +
    `<div class="kv"><span>资产盘</span><span>${esc(S.cfg.asset_root)}</span></div>` +
    `<div class="kv"><span>资产盘可写</span><span>${S.cfg.asset_root_writable ? "是" : "否"}</span></div>`;
}

function clearKeyInput() {
  const input = $("#setKey");
  input.value = "";
  input.type = "password";
  $("#setKeyShow").checked = false;
}

async function saveSessionKey() {
  const value = $("#setKey").value.trim();
  if (value.length < 20) {
    toast("API key 看起来不完整，未保存", "err");
    return;
  }
  const res = await api("/api/key", { method: "POST", body: { api_key: value } });
  clearKeyInput();
  S.cfg = await api("/api/config");
  renderStatus();
  renderSettings();
  $("#keyCheck").innerHTML = `<span style="color:var(--teal)">${esc(res.message)}</span>`;
  toast("会话密钥已载入内存", "ok");
}

async function clearSessionKey() {
  await api("/api/key", { method: "DELETE" });
  clearKeyInput();
  S.cfg = await api("/api/config");
  renderStatus();
  renderSettings();
  $("#keyCheck").textContent = "会话密钥已清除。若环境变量里仍有 OPENAI_API_KEY，会继续生效。";
  toast("会话密钥已清除", "ok");
}

async function checkKey() {
  const box = $("#keyCheck");
  box.textContent = "正在通过代理检查 /v1/models …";
  const res = await api("/api/key/check", { method: "POST", body: {} });
  const color = res.ok ? "var(--teal)" : "var(--red)";
  box.innerHTML =
    `<span style="color:${color}">${esc(res.message)}</span>` +
    `<div class="mt">HTTP ${esc(res.http_status)} · 代理 ${esc(res.proxy)} · 密钥来源 ${esc(res.key_source)}` +
    `${res.model_count ? " · 可见模型 " + esc(res.model_count) : ""}</div>`;
  toast(res.ok ? "连接正常" : "连接检查未通过", res.ok ? "ok" : "err");
}

async function applySettings() {
  const res = await api("/api/settings", {
    method: "POST",
    body: { api_proxy: $("#setProxy").value.trim(), api_base: $("#setBase").value.trim() },
  });
  S.cfg = res.config;
  renderSettings();
  toast("已应用到当前进程（重启后恢复 config.json 的值）", "ok");
}

async function loadAssets() {
  const p = new URLSearchParams({
    q: $("#fQ").value.trim(),
    style: $("#fStyle").value,
    status: $("#fStatus").value,
    min_rating: $("#fRating").value,
    sort: $("#fSort").value,
    favorite: $("#fFav").checked ? "1" : "",
  });
  const data = await api("/api/assets?" + p.toString());
  S.assets = data.assets || [];
  renderGallery();
}

function badge(text, cls) {
  return `<span class="badge ${cls || ""}">${esc(text)}</span>`;
}

function renderGallery() {
  const keepers = S.assets.filter((a) => a.status === "keeper").length;
  const demos = S.assets.filter((a) => a.demo).length;
  $("#gallerySummary").textContent = `共 ${S.assets.length} 项 · 精选 ${keepers} · 演示图 ${demos}`;
  const grid = $("#grid");
  if (!S.assets.length) {
    grid.innerHTML = `<div class="empty">还没有资产。到「批量生成」用 Dry run 跑 30 张，先把流程走通。</div>`;
    return;
  }
  grid.innerHTML = S.assets.map((a) => {
    const tags = [badge(a.style_name || a.style_id, "")];
    if (a.demo) tags.push(badge("演示", "demo"));
    if (a.status === "keeper") tags.push(badge("精选", "keeper"));
    if (a.status === "rejected") tags.push(badge("已淘汰", "rejected"));
    if (a.favorite) tags.push(badge("收藏", "fav"));
    if (a.rating) tags.push(badge("★".repeat(a.rating), ""));
    (a.tags || []).slice(0, 3).forEach((t) => tags.push(badge(t, "")));
    return `<div class="card" data-action="open-asset" data-id="${esc(a.id)}">
      <img class="card-img" src="/media/${esc(a.file)}" loading="lazy" alt="">
      <div class="card-body">
        <div class="card-title">${esc(a.label || a.style_name || a.style_id)}</div>
        <div class="card-meta"><span>${esc(a.asset_kind || a.quality)}</span><span>${a.demo ? "Dry run" : (a.source === "imagegen" ? "内部生成" : "$" + a.cost_usd)}</span></div>
        <div class="badges">${tags.join("")}</div>
      </div>
    </div>`;
  }).join("");
}

function switchView(name) {
  if (!VIEW_META[name]) return;
  S.view = name;
  $$(".view").forEach((el) => el.classList.add("hidden"));
  $("#view-" + name).classList.remove("hidden");
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $("#viewTitle").textContent = VIEW_META[name][0];
  $("#viewSubtitle").textContent = VIEW_META[name][1];
  if (name === "styles") renderStyles();
  if (name === "cost") renderCost();
  if (name === "batch") { refreshBatchEstimate(); }
  if (name === "export") { loadHealth(); loadExports(); renderSyncInfo(); }
  if (name === "settings") { renderSettings(); }
}

function renderStyles() {
  $("#styleList").innerHTML = S.styles.map((s) => `
    <div class="style-card ${s.id === S.styleId ? "sel" : ""}" data-action="pick-style" data-id="${esc(s.id)}">
      <div class="row">
        <span class="style-name">${esc(s.name)}</span>
        <span class="pill ${esc(s.status || "")}">${esc(s.status || "")}</span>
      </div>
      <div class="style-desc">${esc(s.description || "")}</div>
      <div class="swatches">${(s.palette || []).map((c) => `<span class="swatch" style="background:${esc(c)}"></span>`).join("")}</div>
    </div>`).join("") || `<div class="empty">没有风格配方</div>`;

  const style = currentStyle();
  $("#editingStyle").textContent = style.name || "";
  const blocks = (style.blocks || []).map((b) => `
    <div class="block" data-key="${esc(b.key)}">
      <div class="block-head"><span>${esc(b.zh || b.key)}</span><span class="key">${esc(b.key)}</span></div>
      <textarea class="textarea" data-block="${esc(b.key)}">${esc(b.en || "")}</textarea>
    </div>`).join("");
  $("#styleEditor").innerHTML = `
    <div class="info-box">${esc(style.notes || "")}</div>
    <div class="section-label">提示词分块（按顺序拼接，留空即禁用该块）</div>
    <div id="styleBlocks">${blocks}</div>
    <button class="btn ghost" data-action="add-block">+ 增加分块</button>
    <div class="section-label">负面词</div>
    <textarea class="textarea" id="sNegative">${esc(style.negative || "")}</textarea>
    <div class="section-label">实时拼接预览</div>
    <div class="prompt-preview" id="sPreview"></div>
    <div class="mt" id="sEval"></div>
    <div class="btn-row">
      <button class="btn primary" data-action="save-style">保存配方</button>
      <button class="btn" data-action="set-active">设为当前风格</button>
      <button class="btn ghost" data-action="save-style-version">存提示词版本</button>
    </div>`;
  updateStylePreview();
}

function collectStyleBlocks() {
  return $$("#styleBlocks .block").map((box) => ({
    key: box.dataset.key,
    zh: ($(".block-head span", box) || {}).textContent || box.dataset.key,
    en: $("textarea", box).value,
    locked: false,
  }));
}

function updateStylePreview() {
  const style = currentStyle();
  const draft = Object.assign({}, style, {
    blocks: collectStyleBlocks(),
    negative: $("#sNegative") ? $("#sNegative").value : style.negative,
  });
  const prompt = buildPrompt(draft, {}, firstSlots(draft), "");
  const el = $("#sPreview");
  if (el) el.textContent = prompt;
  if ($("#sEval")) renderEval($("#sEval"), draft, prompt);
}

async function saveStyle() {
  const style = currentStyle();
  const payload = {
    blocks: collectStyleBlocks(),
    negative: $("#sNegative").value,
    description: style.description,
    notes: style.notes,
    status: style.status,
  };
  await api("/api/styles/" + encodeURIComponent(style.id), { method: "PUT", body: payload });
  const fresh = await api("/api/styles");
  S.styles = fresh.styles;
  toast("配方已保存", "ok");
  renderStyles();
}

async function setActive(id) {
  for (const s of S.styles) {
    const status = s.id === id ? "active" : "queued";
    if (s.status !== status) {
      await api("/api/styles/" + encodeURIComponent(s.id), { method: "PUT", body: { status } });
    }
  }
  const fresh = await api("/api/styles");
  S.styles = fresh.styles;
  S.styleId = id;
  fillStyleSelects();
  $("#bStyle").value = id;
  toast("已切换当前风格", "ok");
  renderStyles();
  refreshBatchEstimate();
}

function modelOptions() {
  const table = (S.pricing && S.pricing.image) || {};
  return Object.keys(table).map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
}

async function initBatch() {
  $("#bModel").innerHTML = modelOptions();
  $("#bModel").value = S.cfg.default_model || "gpt-image-1.5";
  refreshBatchEstimate();
}

function refreshBatchEstimate() {
  if (!S.pricing) return;
  const style = S.styles.find((s) => s.id === $("#bStyle").value) || currentStyle();
  const model = $("#bModel").value || S.cfg.default_model;
  const qualities = (S.pricing.image[model] || { low: 0, medium: 0, high: 0 });
  const currentQ = $("#bQuality").value;
  $("#bQuality").innerHTML = Object.keys(qualities)
    .map((q) => `<option value="${esc(q)}">${esc(q)} · $${qualities[q]}/张</option>`).join("");
  $("#bQuality").value = qualities[currentQ] !== undefined ? currentQ : (style.model || {}).quality || "low";

  const quality = $("#bQuality").value;
  const count = $("#bCount").value;
  const batch = $("#bBatch").checked;
  const cost = estimateOf(model, quality, count, batch);
  const dry = $("#bDry").checked;
  $("#bEstimate").innerHTML =
    `<div class="num">$${cost}</div><div class="cap">${esc(count)} 张 × ${esc(model)} / ${esc(quality)}` +
    `${batch ? " · Batch 半价估算" : ""} · ${dry ? "Dry run 不产生实际费用" : "真实生成"}</div>`;

  const draft = Object.assign({}, style);
  const prompt = buildPrompt(draft, {}, firstSlots(draft), $("#bExtra").value);
  $("#bPreview").textContent = prompt;
  renderEval($("#bEval"), draft, prompt);
  $("#bHint").textContent = dry
    ? "Dry run 会在本地生成演示图（SVG），可完整走通画廊、编辑、复现和导出，不调用 API。"
    : "真实生成会调用 OpenAI API。系统会检查单次上限与月度预算，超限直接拒绝。";
}

// ---- Live generation progress -------------------------------------------
// The server writes each asset to meta.jsonl the moment that image is ready,
// so polling /api/assets gives us real progress without restarting the server
// (and without losing the in-memory API key). Refreshing the page is safe.
const RUN_KEY = "ai-studio-run-v1";
const RUN = { total: 0, startedMs: 0, finishedAt: 0, error: "" };
let runFetchInFlight = false;
let runTimer = null;
let lastSeenRunCount = -1;

function saveRunMeta() {
  try {
    if (!RUN.total) localStorage.removeItem(RUN_KEY);
    else localStorage.setItem(RUN_KEY, JSON.stringify({
      total: RUN.total, startedMs: RUN.startedMs,
      finishedAt: RUN.finishedAt, error: RUN.error,
    }));
  } catch (_) { /* storage unavailable */ }
}

function loadRunMeta() {
  try {
    const d = JSON.parse(localStorage.getItem(RUN_KEY) || "null");
    if (!d || !d.total) return;
    RUN.total = Number(d.total) || 0;
    RUN.startedMs = Number(d.startedMs) || 0;
    RUN.finishedAt = 0;         // a reload means no live fetch to rely on
    RUN.error = "";
  } catch (_) { /* ignore */ }
}

function setRunButton(busy) {
  const btn = document.querySelector('[data-action="run-batch"]');
  if (!btn) return;
  btn.disabled = busy;
  btn.textContent = busy ? "生成中…（看下方进度）" : "开始生成";
}

function inferRun(assets) {
  const gen = (assets || [])
    .filter((a) => (a.source === "generate" || a.source === "reproduce")
      && Number.isFinite(Date.parse(a.created_at)))
    .sort((x, y) => Date.parse(x.created_at) - Date.parse(y.created_at));
  if (!gen.length) return null;
  // Assets less than 150s apart belong to the same batch.
  let start = gen.length - 1;
  while (start > 0 && Date.parse(gen[start].created_at)
         - Date.parse(gen[start - 1].created_at) <= 150000) start--;
  const rows = gen.slice(start);
  return {
    rows,
    first: Date.parse(rows[0].created_at),
    last: Date.parse(rows[rows.length - 1].created_at),
  };
}

function fmtDuration(sec) {
  if (!Number.isFinite(sec) || sec < 0) sec = 0;
  return sec < 60 ? Math.round(sec) + " 秒" : (sec / 60).toFixed(1) + " 分钟";
}

async function pollRun() {
  let data;
  try {
    data = await api("/api/assets?limit=120&sort=newest");
  } catch (_) { return; }   // server busy generating: retry on next tick
  renderRun(inferRun(data.assets));
}

function renderRun(batch) {
  const chip = $("#runChip");
  const box = $("#runBox");
  if (!batch) { chip.classList.add("hidden"); box.classList.add("hidden"); return; }

  const now = Date.now();
  const idleSec = Math.max(0, (now - batch.last) / 1000);
  const done = batch.rows.length;
  const hasTotal = RUN.total > 0 && RUN.startedMs > 0
    && batch.first >= RUN.startedMs - 15000;
  const total = hasTotal ? RUN.total : 0;
  const explicitlyFinished = RUN.finishedAt > 0 && RUN.finishedAt >= batch.last - 5000;
  const running = !explicitlyFinished && (runFetchInFlight || idleSec < 90);
  const stale = !running && idleSec > 600;

  if (stale) { chip.classList.add("hidden"); box.classList.add("hidden"); return; }
  chip.classList.remove("hidden");
  box.classList.remove("hidden");

  const state = RUN.error && explicitlyFinished ? "生成失败"
    : (explicitlyFinished ? "已完成" : (running ? "生成中" : "本批已结束"));
  const cls = (RUN.error && explicitlyFinished) ? "err" : (running ? "" : "done");
  $("#runState").innerHTML = `<span class="${cls}">${running ? "● " : ""}${esc(state)}</span>`;
  $("#runCount").textContent = total ? `${done} / ${total} 张` : `${done} 张`;

  const bar = $("#runBar");
  if (total) {
    bar.className = "";
    bar.style.width = Math.min(100, Math.round(100 * done / total)) + "%";
  } else {
    bar.className = running ? "indeterminate" : "";
    bar.style.width = running ? "" : "100%";
  }

  const cost = batch.rows.reduce((s, a) => s + (Number(a.cost_usd) || 0), 0);
  const avg = done > 1 ? (batch.last - batch.first) / 1000 / (done - 1) : 0;
  const remain = total && running && avg ? Math.max(0, total - done) * avg : 0;
  const first = batch.rows[0];
  const lines = [
    `风格 ${esc(first.style_name || first.style_id)} · ${esc(first.model || "")} / ${esc(first.quality || "")}`,
    `已计入成本 $${cost.toFixed(4)} · 最近一张 ${fmtDuration(idleSec)}前`,
  ];
  if (avg) lines.push(`平均 ${fmtDuration(avg)}/张${remain ? " · 预计还需 " + fmtDuration(remain) : ""}`);
  if (!total && running) lines.push("本批总数未记录（页面曾刷新），进度按已落盘图片实时统计。");
  if (!total && !running) lines.push("本批总数未记录；下一批开始会记住总张数并显示百分比进度。");
  if (RUN.error) lines.push(`错误：${esc(RUN.error)}`);
  $("#runStats").innerHTML = lines.join("<br>");
  $("#runThumbs").innerHTML = batch.rows.slice(-5).reverse()
    .map((a) => `<img src="/media/${esc(a.file)}" alt="" title="${esc(a.id)}">`).join("");

  chip.innerHTML = `<span class="dot ${running ? "ok" : ""}"></span>`
    + `${running ? "生成中" : "生成完成"} ${done}${total ? "/" + total : " 张"}`;

  if (done !== lastSeenRunCount) {
    if (lastSeenRunCount >= 0 && S.view === "gallery") loadAssets();
    lastSeenRunCount = done;
  }
}

function startRunPolling() {
  clearInterval(runTimer);
  pollRun();
  runTimer = setInterval(pollRun, 3000);
}

async function runBatch() {
  if (runFetchInFlight) {
    toast("已有一批正在生成，等它结束再启动新的一批", "err");
    return;
  }
  const body = {
    style_id: $("#bStyle").value,
    model: $("#bModel").value,
    quality: $("#bQuality").value,
    count: Number($("#bCount").value),
    extra: $("#bExtra").value,
    dry_run: $("#bDry").checked,
    batch_estimate: $("#bBatch").checked,
  };
  const cost = estimateOf(body.model, body.quality, body.count, body.batch_estimate);
  const go = async () => {
    runFetchInFlight = true;
    RUN.total = body.count;
    RUN.startedMs = Date.now();
    RUN.finishedAt = 0;
    RUN.error = "";
    saveRunMeta();
    setRunButton(true);
    pollRun();
    try {
      const res = await api("/api/generate", { method: "POST", body });
      toast(`已生成 ${res.count} 张${res.dry_run ? "演示图" : "图片"}`, "ok");
      await refreshAll();
      await loadAssets();
    } catch (err) {
      RUN.error = err.message;
      toast(err.message, "err");
    } finally {
      runFetchInFlight = false;
      RUN.finishedAt = Date.now();
      saveRunMeta();
      setRunButton(false);
      await pollRun();
    }
  };
  if (body.dry_run) await go();
  else confirmModal("确认真实生成",
    `将调用 API 生成 <b>${body.count}</b> 张，预估 <b>$${cost}</b>。<br><br>每张约十几到几十秒，期间下方进度面板会实时更新，请勿重复点击。`,
    go);
}

async function refreshAll() {
  const [cfg, stats, styles] = await Promise.all([
    api("/api/config"), api("/api/stats"), api("/api/styles"),
  ]);
  S.cfg = cfg; S.stats = stats; S.styles = styles.styles;
  renderStatus();
  await loadAssets();
  renderCost();
}

function renderCost() {
  if (!S.stats) return;
  const st = S.stats;
  const cards = [
    ["月度额度", "$" + st.budget_usd, ""],
    ["已用（真实调用）", "$" + st.real_cost_usd, "teal"],
    ["剩余", "$" + st.remaining_usd, ""],
    ["资产总数", st.total, ""],
    ["精选", st.keepers, ""],
    ["演示图", st.demos, ""],
  ];
  $("#costCards").innerHTML = cards.map(([l, v, c]) =>
    `<div class="stat-card"><div class="label">${esc(l)}</div><div class="value ${c}">${esc(v)}</div></div>`).join("");

  const row = (k, v) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`;
  $("#costBreakdown").innerHTML =
    `<div class="section-label">按风格</div>` +
    Object.entries(st.by_style || {}).map(([k, v]) => row(k, v + " 张")).join("") +
    `<div class="section-label mt">按模型</div>` +
    Object.entries(st.by_model || {}).map(([k, v]) => row(k, v + " 张")).join("") +
    `<div class="section-label mt">按月份花费</div>` +
    Object.entries(st.by_month || {}).map(([k, v]) => row(k, "$" + v)).join("");

  const table = (S.pricing && S.pricing.image) || {};
  $("#priceTable").innerHTML = `<table><tr><th>模型</th><th>low</th><th>medium</th><th>high</th></tr>` +
    Object.entries(table).map(([m, q]) =>
      `<tr><td>${esc(m)}</td><td>$${q.low}</td><td>$${q.medium}</td><td>$${q.high}</td></tr>`).join("") +
    `</table><div class="hint">单价来自 OpenAI 图像生成定价页（2026-11 核对）。Batch 半价只用于估算，实际以账单为准。</div>`;
}

function renderSyncInfo() {
  const ok = S.cfg.asset_root_writable;
  $("#syncInfo").innerHTML = ok
    ? `资产根目录 <b>${esc(S.cfg.asset_root)}</b> 可写。精选资产会按批次文件夹整体复制过去，再由 Google Drive 同步到云端。`
    : `资产根目录 <b>${esc(S.cfg.asset_root)}</b> 当前不可写。资产仍安全存放在本地 data/，同步会明确报错。`;
}

async function loadHealth() {
  const data = await api("/api/health");
  const box = $("#healthBox");
  if (!box) return;
  box.innerHTML = `<div class="info-box">已检查 ${data.checked} 项，${data.healthy ? "未发现问题" : `发现 ${data.issues.length} 处提示`}</div>` +
    (data.issues || []).map((i) =>
      `<div class="list-row"><span>${esc(i.id)}</span><span class="muted">${esc(i.issue)}</span></div>`).join("");
}

async function loadExports() {
  const data = await api("/api/exports");
  $("#exportList").innerHTML = (data.exports || []).map((f) =>
    `<div class="list-row"><a href="/media/export/${encodeURIComponent(f.name)}" style="color:var(--blue)">${esc(f.name)}</a><span class="muted">${(f.size / 1024).toFixed(1)} KB</span></div>`).join("")
    || `<div class="muted small">还没有导出记录</div>`;
}

async function doExport() {
  const scope = $("#eScope").value;
  const body = { name: $("#eName").value.trim() || undefined };
  if (scope === "keeper") body.status = "keeper";
  if (scope === "fav") body.favorite = true;
  if (scope === "all") {
    body.q = $("#fQ").value.trim();
    body.style_id = $("#fStyle").value;
    body.status = $("#fStatus").value;
    body.min_rating = Number($("#fRating").value);
    body.sort = $("#fSort").value;
    body.favorite = $("#fFav").checked;
  }
  const res = await api("/api/export", { method: "POST", body });
  toast(res.count ? `已导出 ${res.count} 张：${res.filename}` : "没有可导出的资产", res.count ? "ok" : "");
  await loadExports();
}

async function doSync() {
  const style = $("#eSyncStyle") ? $("#eSyncStyle").value : "";
  const includeDemo = $("#eIncludeDemo") ? $("#eIncludeDemo").checked : false;
  const res = await api("/api/sync", { method: "POST", body: {
    style_id: style || undefined, include_demo: includeDemo,
  } });
  if (!res.ok) { toast(res.reason || "同步失败", "err"); return; }
  toast(res.copied ? `已同步 ${res.copied} 张到 ${res.batch}` : (res.reason || "没有可同步的精选资产"),
        res.copied ? "ok" : "");
  S.cfg = await api("/api/config");
  renderStatus();
  renderSyncInfo();
}

async function openAsset(id) {
  const a = await api("/api/assets/" + encodeURIComponent(id));
  S.drawerAsset = a;
  S.assetId = a.id;
  $("#drawer").classList.remove("hidden");
  $("#dTitle").textContent = a.label || a.style_name || a.id;
  $("#dId").textContent = a.id;
  renderDrawer(a);
  await loadVersions(a.style_id);
}

function collectDrawerBlocks() {
  const out = {};
  $$("[data-dblock]", $("#drawerBody")).forEach((t) => { out[t.dataset.dblock] = t.value; });
  return out;
}

function renderDrawer(a) {
  const style = S.styles.find((s) => s.id === a.style_id) || { blocks: [] };
  const ov = a.blocks || {};
  const blocksHtml = (style.blocks || []).map((b) => {
    const val = ov[b.key] !== undefined ? ov[b.key] : b.en;
    return `<div class="block"><div class="block-head"><span>${esc(b.zh || b.key)}</span><span class="key">${esc(b.key)}</span></div>
      <textarea class="textarea" data-dblock="${esc(b.key)}">${esc(val)}</textarea></div>`;
  }).join("");

  const table = (S.pricing && S.pricing.image) || {};
  const modelOpts = Object.keys(table).map((m) =>
    `<option value="${esc(m)}" ${m === a.model ? "selected" : ""}>${esc(m)}</option>`).join("");
  const qOpts = Object.keys(table[a.model] || { low: 0, medium: 0, high: 0 }).map((q) =>
    `<option value="${esc(q)}" ${q === a.quality ? "selected" : ""}>${esc(q)}</option>`).join("");
  const stars = [1, 2, 3, 4, 5].map((n) =>
    `<button class="star ${n <= (a.rating || 0) ? "on" : ""}" data-action="rate" data-n="${n}">★</button>`).join("");
  const statusBadge = a.status === "keeper" ? badge("精选", "keeper")
    : a.status === "rejected" ? badge("已淘汰", "rejected") : badge("待筛选", "");

  $("#drawerBody").innerHTML = `
    <img class="drawer-img" src="/media/${esc(a.file)}" alt="">
    <div class="badges" style="margin-top:10px">${a.demo ? badge("演示 · 未调用 API", "demo") : ""}${statusBadge}${a.favorite ? badge("收藏", "fav") : ""}</div>

    <div class="section-label">元信息</div>
    <div class="kv"><span>风格</span><span>${esc(a.style_id)}</span></div>
    <div class="kv"><span>模型 / 质量 / 尺寸</span><span>${esc(a.model)} / ${esc(a.quality)} / ${esc(a.size)}</span></div>
    <div class="kv"><span>成本</span><span>${a.demo ? "Dry run" : "$" + a.cost_usd}</span></div>
    <div class="kv"><span>来源 / 版本</span><span>${esc(a.source || "generate")} / v${a.prompt_version || 1}</span></div>
    <div class="kv"><span>来源元数据</span><span>${esc(a.metadata || "unknown")}</span></div>
    <div class="kv"><span>创建时间</span><span>${esc(a.created_at)}</span></div>

    <div class="section-label">提示词分块</div>
    <div id="dBlocks">${blocksHtml || `<div class="muted small">该风格没有分块配置</div>`}</div>
    <button class="btn ghost" data-action="rebuild-prompt">按分块重建最终提示词</button>

    <div class="section-label">最终提示词（可直接改）</div>
    <textarea class="textarea" id="dPrompt" style="min-height:150px">${esc(a.prompt || "")}</textarea>

    <div class="section-label">复现参数</div>
    <div class="form">
      <label>模型<select class="select" id="dModel">${modelOpts}</select></label>
      <label>质量档<select class="select" id="dQuality">${qOpts}</select></label>
      <label class="check"><input type="checkbox" id="dDry" checked> Dry run（不调用 API）</label>
      <label class="check"><input type="checkbox" id="dBatch"> 按 Batch 半价估算</label>
    </div>
    <div class="btn-row">
      <button class="btn primary" data-action="reproduce">复现生成</button>
      <button class="btn" data-action="save-asset">保存修改</button>
      <button class="btn ghost" data-action="save-version">存为版本</button>
    </div>

    <div class="section-label">管理</div>
    <div class="stars">${stars}</div>
    <div class="form mt">
      <label>标签（逗号分隔）<input class="input" id="dTags" value="${esc((a.tags || []).join(", "))}"></label>
      <label>备注<textarea class="textarea" id="dNote" style="min-height:54px">${esc(a.note || "")}</textarea></label>
      <label class="check"><input type="checkbox" id="dFav" ${a.favorite ? "checked" : ""}> 收藏</label>
    </div>
    <div class="btn-row">
      <button class="btn" data-action="keep">标为精选</button>
      <button class="btn ghost" data-action="reject">淘汰</button>
      <button class="btn ghost" data-action="restore">恢复待筛选</button>
    </div>

    <div class="section-label">提示词版本历史</div>
    <div id="dVersions" class="list"><div class="muted small">加载中</div></div>`;
}

async function loadVersions(styleId) {
  const data = await api("/api/versions?style=" + encodeURIComponent(styleId));
  S.versions = data.versions || [];
  const box = $("#dVersions");
  if (!box) return;
  box.innerHTML = S.versions.slice(0, 12).map((v, i) =>
    `<div class="list-row"><span>${esc(v.created_at)}<br><span class="muted small">${esc(v.note || "saved")}</span></span>
     <button class="btn ghost" data-action="apply-version" data-i="${i}">应用</button></div>`).join("")
    || `<div class="muted small">还没有版本记录</div>`;
}

async function patchAsset(patch) {
  const a = S.drawerAsset;
  if (!a) return;
  await api("/api/assets/" + encodeURIComponent(a.id), { method: "PATCH", body: patch });
  await loadAssets();
  await openAsset(a.id);
}

async function saveAssetEdits() {
  const tags = $("#dTags").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
  await patchAsset({
    prompt: $("#dPrompt").value,
    blocks: collectDrawerBlocks(),
    rating: S.drawerAsset.rating || 0,
    tags,
    note: $("#dNote").value,
    favorite: $("#dFav").checked,
  });
  toast("资产信息已保存", "ok");
}

async function saveVersion(note) {
  const a = S.drawerAsset;
  await api("/api/styles/" + encodeURIComponent(a.style_id) + "/version", {
    method: "POST",
    body: {
      asset_id: a.id, prompt: $("#dPrompt").value,
      blocks: collectDrawerBlocks(), note: note || "from asset editor",
    },
  });
  toast("提示词版本已保存", "ok");
  await loadVersions(a.style_id);
}

async function reproduce() {
  const a = S.drawerAsset;
  const model = $("#dModel").value;
  const quality = $("#dQuality").value;
  const dry = $("#dDry").checked;
  const batch = $("#dBatch").checked;
  const cost = estimateOf(model, quality, 1, batch);
  const go = async () => {
    const res = await api("/api/assets/" + encodeURIComponent(a.id) + "/reproduce", {
      method: "POST",
      body: {
        prompt: $("#dPrompt").value.trim(), blocks: collectDrawerBlocks(),
        model, quality, dry_run: dry, batch_estimate: batch,
      },
    });
    toast(res.demo ? "已生成演示图" : "已生成新图", "ok");
    await refreshAll();
    await openAsset(res.asset.id);
  };
  if (dry) await go();
  else confirmModal("确认真实生成", `将调用 API，预估 <b>$${cost}</b>。`, go);
}

async function boot() {
  try {
    const [cfg, styles, pricing, stats] = await Promise.all([
      api("/api/config"), api("/api/styles"), api("/api/pricing"), api("/api/stats"),
    ]);
    S.cfg = cfg; S.styles = styles.styles || []; S.pricing = pricing; S.stats = stats;
    S.styleId = (S.styles.find((s) => s.status === "active") || S.styles[0] || {}).id || "";
    fillStyleSelects();
    renderStatus();
    renderCost();
    await initBatch();
    await loadAssets();
    await loadExports();
    switchView("gallery");
    loadRunMeta();
    startRunPolling();
  } catch (err) {
    toast("初始化失败：" + err.message, "err");
  }
}

document.addEventListener("click", async (e) => {
  const nav = e.target.closest("[data-view]");
  if (nav) { switchView(nav.dataset.view); return; }

  const el = e.target.closest("[data-action]");
  if (!el) return;
  const act = el.dataset.action;
  try {
    switch (act) {
      case "refresh": await loadAssets(); break;
      case "goto-batch": switchView("batch"); break;
      case "open-asset": await openAsset(el.dataset.id); break;
      case "close-drawer": $("#drawer").classList.add("hidden"); S.drawerAsset = null; break;
      case "modal-cancel": $("#modal").classList.add("hidden"); S.modalCb = null; break;
      case "modal-ok": {
        const cb = S.modalCb;
        $("#modal").classList.add("hidden"); S.modalCb = null;
        if (cb) await cb();
        break;
      }
      case "sync": switchView("export"); await doSync(); break;
      case "run-batch": await runBatch(); break;
      case "do-export": await doExport(); break;
      case "do-sync": await doSync(); break;
      case "refresh-health": await loadHealth(); break;
      case "save-session-key": await saveSessionKey(); break;
      case "clear-session-key": await clearSessionKey(); break;
      case "check-key": await checkKey(); break;
      case "apply-settings": await applySettings(); break;
      case "pick-style": S.styleId = el.dataset.id; renderStyles(); break;
      case "add-block": {
        const n = $$("#styleBlocks .block").length + 1;
        const box = document.createElement("div");
        box.className = "block";
        box.dataset.key = "custom_" + n;
        box.innerHTML = `<div class="block-head"><span>自定义 ${n}</span><span class="key">custom_${n}</span></div><textarea class="textarea" data-block="custom_${n}"></textarea>`;
        $("#styleBlocks").appendChild(box);
        updateStylePreview();
        break;
      }
      case "save-style": await saveStyle(); break;
      case "set-active": await setActive(S.styleId); break;
      case "save-style-version": {
        const style = currentStyle();
        const draft = Object.assign({}, style, { blocks: collectStyleBlocks(), negative: $("#sNegative").value });
        await api("/api/styles/" + encodeURIComponent(style.id) + "/version", {
          method: "POST", body: { prompt: buildPrompt(draft, {}, firstSlots(draft), ""), blocks: {}, note: "style editor" },
        });
        toast("提示词版本已保存", "ok");
        break;
      }
      case "rebuild-prompt": {
        const a = S.drawerAsset;
        const style = S.styles.find((s) => s.id === a.style_id) || { blocks: [] };
        const draft = Object.assign({}, style, { blocks: (style.blocks || []).map((b) => ({ ...b, en: collectDrawerBlocks()[b.key] || "" })) });
        $("#dPrompt").value = buildPrompt(draft, {}, a.slots || {}, a.extra || "");
        toast("已按分块重建提示词", "ok");
        break;
      }
      case "reproduce": await reproduce(); break;
      case "save-asset": await saveAssetEdits(); break;
      case "save-version": await saveVersion(); break;
      case "rate": {
        const n = Number(el.dataset.n);
        await patchAsset({ rating: n === S.drawerAsset.rating ? 0 : n });
        break;
      }
      case "keep": await patchAsset({ status: "keeper" }); toast("已标为精选", "ok"); break;
      case "reject": await patchAsset({ status: "rejected" }); toast("已淘汰", "ok"); break;
      case "restore": await patchAsset({ status: "raw" }); toast("已恢复", "ok"); break;
      case "apply-version": {
        const v = S.versions[Number(el.dataset.i)];
        if (v && $("#dPrompt")) { $("#dPrompt").value = v.prompt; toast("已应用该版本（记得保存）", "ok"); }
        break;
      }
      default: break;
    }
  } catch (err) {
    toast(err.message, "err");
  }
});

let searchTimer = null;
document.addEventListener("input", (e) => {
  if (e.target.id === "fQ") {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadAssets, 260);
  }
  if (e.target.closest("#styleEditor")) updateStylePreview();
});

document.addEventListener("change", (e) => {
  const id = e.target.id;
  if (id === "setKeyShow") $("#setKey").type = e.target.checked ? "text" : "password";
  if (["fStyle", "fStatus", "fRating", "fSort", "fFav"].includes(id)) loadAssets();
  if (["bStyle", "bModel", "bQuality", "bCount", "bExtra", "bDry", "bBatch"].includes(id)) {
    if (id === "bModel" || id === "bStyle") {
      const style = S.styles.find((s) => s.id === $("#bStyle").value);
      if (style && id === "bStyle") {
        $("#bModel").value = (style.model || {}).default || S.cfg.default_model;
      }
    }
    refreshBatchEstimate();
  }
});

boot();
