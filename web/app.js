"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ESC[c]);

const S = {
  cfg: {}, styles: [], pricing: null, stats: null, assets: [],
  view: "gallery", styleId: "", assetId: null, drawerAsset: null,
  modalCb: null, versions: [],
  assetsTotal: 0, assetsOffset: 0, assetsHasMore: false, assetsLoading: false,
};
const PAGE_SIZE = 60;

// Character / game-asset pipeline state, kept separate from the gallery state.
const G = { meta: null, cards: [], extracted: [], card: null, assets: [], packs: [] };

const VIEW_META = {
  gallery: ["资产画廊", "查看、筛选、复现与管理所有美术资产"],
  styles: ["风格配方", "编辑分块提示词、变量槽位与负面词"],
  batch: ["批量生成", "先 Dry run 验证配方，再切换真实生成"],
  game: ["角色与游戏资产", "小说提取角色、锚点复现、多视角与精灵动画切图"],
  export: ["导出与同步", "导出素材包、同步精选资产、检查元数据"],
  cost: ["成本看板", "额度、用量与参考单价"],
  settings: ["连接设置", "会话密钥、代理与端点自检"],
  help: ["使用说明", "零依赖本地运行，SQLite 索引 + 加密凭据 + 缩略图"],
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
  const srcLabel = {
    session: "会话密钥（仅内存）",
    disk: "本机密钥（已记住）",
    file: "config.json 明文密钥",
    env: "环境变量密钥",
  }[S.cfg.api_key_source] || "密钥已设置";
  $("#txtKey").textContent = keyOk ? srcLabel : "未设密钥（仅 Dry run）";
  const diskOk = !!S.cfg.asset_root_writable;
  $("#dotDisk").className = "dot " + (diskOk ? "ok" : "bad");
  $("#txtDisk").textContent = diskOk ? "资产盘可写" : "资产盘不可写";
  const used = S.stats ? S.stats.real_cost_usd : 0;
  $("#budgetChip").textContent = `本月 $${used} / $${S.cfg.monthly_budget_usd}`;
}

const KEY_SOURCE_LABEL = {
  session: "会话密钥（仅内存，重启失效）",
  disk: "本机加密保存的密钥（重启自动生效）",
  file: "config.json 里的明文密钥（重启自动生效）",
  env: "环境变量 OPENAI_API_KEY",
  none: "未设置",
};

function renderSettings() {
  const src = S.cfg.api_key_source || "none";
  $("#keyState").innerHTML =
    `<div class="kv"><span>当前生效密钥</span><span>${S.cfg.api_key_set ? esc(KEY_SOURCE_LABEL[src] || src) : "未设置"}</span></div>` +
    `<div class="kv"><span>本机记住的密钥</span><span>${S.cfg.api_key_persisted ? esc(S.cfg.api_key_hint || "已保存") : "无"}</span></div>` +
    `<div class="kv" title="${esc(S.cfg.credential_path || "")}"><span>凭据文件</span><span>${S.cfg.api_key_persisted ? esc(S.cfg.credential_cipher) + " 加密 · 仓库外" : "未创建"}</span></div>` +
    `<div class="kv"><span>代理 api_proxy</span><span>${esc(S.cfg.api_proxy || "(系统默认)")}</span></div>` +
    `<div class="kv"><span>API base</span><span>${esc(S.cfg.api_base || "https://api.openai.com/v1")}</span></div>`;
  $("#setProxy").value = S.cfg.api_proxy || "";
  $("#setBase").value = S.cfg.api_base || "";
  const ignored = !!S.cfg.config_git_ignored;
  $("#fileKeyState").innerHTML =
    `<div class="kv"><span>config.json 明文密钥</span><span>${S.cfg.api_key_in_file ? esc(S.cfg.api_key_file_hint || "已写入") : "无"}</span></div>` +
    `<div class="kv" title="${esc(S.cfg.config_path || "")}"><span>文件位置</span><span>项目根目录 config.json</span></div>` +
    `<div class="kv"><span>已被 .gitignore 排除</span><span style="color:${ignored ? "var(--teal)" : "var(--red)"}">${ignored ? "是（不会提交）" : "否（危险：可能被提交！）"}</span></div>` +
    `<div class="kv"><span>当前生效来源</span><span>${esc(KEY_SOURCE_LABEL[S.cfg.api_key_source] || S.cfg.api_key_source || "未设置")}</span></div>`;
  $("#settingsInfo").innerHTML =
    `<div class="kv"><span>监听地址</span><span>${esc(S.cfg.host)}:${esc(S.cfg.port)}</span></div>` +
    `<div class="kv"><span>默认模型</span><span>${esc(S.cfg.default_model)} / ${esc(S.cfg.default_quality)}</span></div>` +
    `<div class="kv"><span>资产盘</span><span>${esc(S.cfg.asset_root)}</span></div>` +
    `<div class="kv"><span>资产盘可写</span><span>${S.cfg.asset_root_writable ? "是" : "否"}</span></div>` +
    `<div class="kv"><span>资产索引</span><span>${esc((S.stats || {}).storage || "sqlite")} · 每页 ${PAGE_SIZE} 张 · 缩略图缓存</span></div>`;
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
  const res = await api("/api/key", {
    method: "POST",
    body: { api_key: value, persist: $("#setKeyPersist").checked },
  });
  clearKeyInput();
  S.cfg = await api("/api/config");
  renderStatus();
  renderSettings();
  $("#keyCheck").innerHTML = `<span style="color:var(--teal)">${esc(res.message)}</span>`;
  toast(res.persisted ? "密钥已加密保存到本机" : "会话密钥已载入内存", "ok");
}

async function persistCurrentKey() {
  const res = await api("/api/key/persist", { method: "POST", body: {} });
  S.cfg = await api("/api/config");
  renderStatus();
  renderSettings();
  $("#keyCheck").innerHTML = `<span style="color:var(--teal)">${esc(res.message)} 脱敏标识：${esc((res.credentials || {}).hint || "")}</span>`;
  toast("密钥已加密保存到本机", "ok");
}

async function clearPersistedKey() {
  const res = await api("/api/key/persist", { method: "DELETE" });
  S.cfg = await api("/api/config");
  renderStatus();
  renderSettings();
  $("#keyCheck").textContent = res.message;
  toast("已删除本机记住的密钥", "ok");
}

async function clearSessionKey() {
  await api("/api/key", { method: "DELETE" });
  clearKeyInput();
  S.cfg = await api("/api/config");
  renderStatus();
  renderSettings();
  $("#keyCheck").textContent = "会话密钥已清除。若本机记住了密钥、config.json 里有明文密钥，或环境变量仍有值，会继续生效。";
  toast("会话密钥已清除", "ok");
}

async function saveFileKey() {
  const value = $("#setKey").value.trim();
  if (value.length < 20) {
    toast("请先在上方输入框粘贴完整 API key", "err");
    return;
  }
  if (!S.cfg.config_git_ignored) {
    confirmModal("警告：config.json 未被忽略",
      `当前 <code>.gitignore</code> 没有排除 <code>config.json</code>，明文密钥可能被提交到公开仓库。<br><br>仍要继续写入吗？`,
      () => writeFileKey(value));
    return;
  }
  await writeFileKey(value);
}

async function writeFileKey(value) {
  const res = await api("/api/key/file", { method: "POST", body: { api_key: value } });
  clearKeyInput();
  S.cfg = res.config;
  renderStatus();
  renderSettings();
  $("#keyCheck").innerHTML = `<span style="color:var(--teal)">${esc(res.message)} 脱敏标识：${esc(res.hint || "")}</span>`;
  toast("已明文写入 config.json", "ok");
}

async function clearFileKey() {
  const res = await api("/api/key/file", { method: "DELETE" });
  S.cfg = res.config;
  renderStatus();
  renderSettings();
  $("#keyCheck").textContent = res.message + " 其它配置（如代理）保持不变。";
  toast("已删除 config.json 里的明文密钥", "ok");
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

async function loadAssets(reset = true) {
  if (S.assetsLoading) return;
  if (reset) { S.assetsOffset = 0; S.assets = []; }
  S.assetsLoading = true;
  const p = new URLSearchParams({
    q: $("#fQ").value.trim(),
    style: $("#fStyle").value,
    status: $("#fStatus").value,
    min_rating: $("#fRating").value,
    sort: $("#fSort").value,
    favorite: $("#fFav").checked ? "1" : "",
    character_id: $("#fCharacter") ? $("#fCharacter").value : "",
    limit: String(PAGE_SIZE),
    offset: String(S.assetsOffset),
  });
  try {
    const data = await api("/api/assets?" + p.toString());
    const batch = data.assets || [];
    S.assets = reset ? batch : S.assets.concat(batch);
    S.assetsTotal = Number(data.total || 0);
    S.assetsHasMore = !!data.has_more;
    S.assetsOffset += batch.length;
    renderGallery();
  } finally {
    S.assetsLoading = false;
  }
}

function badge(text, cls) {
  return `<span class="badge ${cls || ""}">${esc(text)}</span>`;
}

function renderGallery() {
  const keepers = S.assets.filter((a) => a.status === "keeper").length;
  const demos = S.assets.filter((a) => a.demo).length;
  $("#gallerySummary").textContent =
    `共 ${S.assetsTotal} 项 · 已加载 ${S.assets.length} 张 · 本页精选 ${keepers} · 演示图 ${demos}`;
  const grid = $("#grid");
  const more = $("#loadMore");
  if (!S.assets.length) {
    grid.innerHTML = `<div class="empty">还没有资产。到「批量生成」用 Dry run 跑 30 张，先把流程走通。</div>`;
    more.innerHTML = "";
    return;
  }
  grid.innerHTML = S.assets.map((a) => {
    const tags = [badge(a.style_name || a.style_id, "")];
    if (a.demo) tags.push(badge("演示", "demo"));
    if (a.status === "keeper") tags.push(badge("精选", "keeper"));
    if (a.status === "rejected") tags.push(badge("已淘汰", "rejected"));
    if (a.favorite) tags.push(badge("收藏", "fav"));
    if (a.rating) tags.push(badge("★".repeat(a.rating), ""));
    if (a.character_id) {
      const c = (G.cards || []).find((x) => x.id === a.character_id);
      tags.push(badge((c ? c.name : "角色") + " v" + (a.anchor_version || 1), "keeper"));
    }
    (a.tags || []).slice(0, 3).forEach((t) => tags.push(badge(t, "")));
    return `<div class="card" data-action="open-asset" data-id="${esc(a.id)}">
      <img class="card-img" src="/thumb/${esc(a.file)}" data-fallback="${esc(a.file)}" loading="lazy" alt="">
      <div class="card-body">
        <div class="card-title">${esc(a.label || a.style_name || a.style_id)}</div>
        <div class="card-meta"><span>${esc(a.asset_kind || a.quality)}</span><span>${a.demo ? "Dry run" : (a.source === "imagegen" ? "内部生成" : "$" + a.cost_usd)}</span></div>
        <div class="badges">${tags.join("")}</div>
      </div>
    </div>`;
  }).join("");
  more.innerHTML = S.assetsHasMore
    ? `<button class="btn" data-action="load-more">加载更多（还有 ${Math.max(0, S.assetsTotal - S.assets.length)} 张）</button>`
    : (S.assets.length > PAGE_SIZE ? `<span>已全部加载（${S.assets.length} 张）</span>` : "");
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
  if (name === "game") { initGame().catch((e) => toast("游戏资产页加载失败：" + e.message, "err")); }
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
    data = await api("/api/assets?limit=200&sort=newest");
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
    // Only auto-refresh while the user is still on the first page, so deep
    // scrolling through a large library is never interrupted by new assets.
    if (lastSeenRunCount >= 0 && S.view === "gallery"
        && S.assetsOffset <= PAGE_SIZE) loadAssets(true);
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

// ---- Character & game-asset pipeline ------------------------------------
const GAME_WIDE_FIELDS = ["outfit", "marks", "art_style", "negative"];

function blankGameCard() {
  return {
    id: "", name: "", aliases: [], source: "", profile: {}, evidence: {},
    confidence: {}, anchor: "", anchor_version: 0, anchor_history: [],
    ref_asset_id: "", mode: "manual",
  };
}

function gameFieldLabel(key) {
  const f = ((G.meta || {}).profile_fields || []).find((x) => x.key === key);
  return f ? f.label : key;
}

function fillCharacterSelects() {
  const sel = $("#fCharacter");
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = `<option value="">全部角色</option>` +
    (G.cards || []).map((c) =>
      `<option value="${esc(c.id)}">${esc(c.name)} · v${esc(c.anchor_version || 1)}</option>`).join("");
  if (keep) sel.value = keep;
}

function buildGameUI() {
  const m = G.meta || {};
  const keepMode = $("#gTurnMode").value;
  const keepModel = $("#gModel").value;
  const keepCell = $("#gCell").value;
  $("#gTurnMode").innerHTML = (m.turnaround_modes || []).map((x) =>
    `<option value="${esc(x.key)}">${esc(x.label)}</option>`).join("");
  if (keepMode) $("#gTurnMode").value = keepMode;
  $("#gModel").innerHTML = modelOptions();
  $("#gModel").value = keepModel || m.default_model || S.cfg.default_model || "gpt-image-1.5";
  $("#gCell").value = keepCell || m.default_cell || 128;
  refreshGameQuality();
  $("#gProfileFields").innerHTML = (m.profile_fields || []).map((f) =>
    `<label class="${GAME_WIDE_FIELDS.includes(f.key) ? "wide" : ""}">
       <span>${esc(f.label)} <span class="conf" data-conf="${esc(f.key)}"></span></span>
       <input class="input" data-field="${esc(f.key)}" placeholder="留空则忽略">
     </label>`).join("");
  $("#gActions").innerHTML = (m.actions || []).map((a, i) =>
    `<label class="opt ${i < 2 ? "on" : ""}"><input type="checkbox" value="${esc(a.key)}"
       data-opt="action" ${i < 2 ? "checked" : ""}>${esc(a.label)}</label>`).join("");
  $("#gEngines").innerHTML = (m.engines || []).map((e) =>
    `<label class="opt on"><input type="checkbox" value="${esc(e)}"
       data-opt="engine" checked>${esc(e)}</label>`).join("");
  $("#gAnchor").placeholder = "留空保存时按上面的特征自动生成锚点；直接编辑则保留手工文本。";
}

function refreshGameQuality() {
  const model = $("#gModel").value;
  const table = (S.pricing && S.pricing.image) || {};
  const qualities = table[model] || { low: 0, medium: 0, high: 0 };
  const keep = $("#gQuality").value;
  $("#gQuality").innerHTML = Object.keys(qualities).map((q) =>
    `<option value="${esc(q)}">${esc(q)} · $${qualities[q]}/张</option>`).join("");
  $("#gQuality").value = qualities[keep] !== undefined
    ? keep : (G.meta || {}).default_quality || "low";
  refreshGameEstimate();
}

function refreshGameEstimate() {
  const model = $("#gModel").value || (G.meta || {}).default_model;
  const quality = $("#gQuality").value || "low";
  const mode = $("#gTurnMode").value || "sheet";
  const n = mode === "separate" ? 4 : 1;
  const unit = priceOf(model, quality);
  const dry = $("#gDry") ? $("#gDry").checked : true;
  $("#gEstimate").innerHTML =
    `<div class="num">$${(unit * n).toFixed(4)}</div>` +
    `<div class="cap">${n} 张 × ${esc(model)} / ${esc(quality)} · ${esc(mode)} 模式` +
    `${dry ? " · Dry run 不产生费用" : " · 真实生成"}</div>` +
    `<div class="cap mt">复现单张约 $${unit.toFixed(4)}；精灵切图完全在本地处理，不调用 API、不花钱。</div>`;
}

async function prepareGame() {
  const [meta, cards, assets] = await Promise.all([
    api("/api/game/meta"), api("/api/characters"),
    api("/api/assets?limit=500&sort=newest"),
  ]);
  G.meta = meta;
  G.cards = cards.characters || [];
  G.assets = assets.assets || [];
  fillCharacterSelects();
  buildGameUI();
}

async function refreshGameAssets() {
  const res = await api("/api/assets?limit=500&sort=newest");
  G.assets = res.assets || [];
  fillGameAssetSelects();
}

function fillGameAssetSelects() {
  const pngs = (G.assets || []).filter((a) => /\.png$/i.test(a.file || ""));
  const opts = pngs.map((a) =>
    `<option value="${esc(a.id)}">${esc(a.label || a.asset_kind || a.id)}` +
    ` · ${esc(a.style_id)} · ${esc(a.id.slice(0, 15))}</option>`).join("");
  $("#gSpriteAsset").innerHTML = opts || `<option value="">（还没有 PNG 资产）</option>`;
  const keep = $("#gRefAsset").value;
  $("#gRefAsset").innerHTML = `<option value="">不使用参考图（仅靠锚点）</option>` + opts;
  if (keep) $("#gRefAsset").value = keep;
}

async function loadGameCharacters(selectId) {
  const res = await api("/api/characters");
  G.cards = res.characters || [];
  fillCharacterSelects();
  $("#gCardSelect").innerHTML = `<option value="">（新建角色）</option>` +
    G.cards.map((c) =>
      `<option value="${esc(c.id)}">${esc(c.name)} · 锚点 v${esc(c.anchor_version || 1)}</option>`).join("");
  let id = selectId || $("#gCardSelect").value ||
    (G.card && G.card.id) || (G.cards[0] && G.cards[0].id) || "";
  if (id && !G.cards.some((c) => c.id === id)) id = "";
  $("#gCardSelect").value = id;
  if (id) await selectGameCard(id);
  else { G.card = blankGameCard(); renderGameCard(); }
}

function renderGameCard() {
  const c = G.card || blankGameCard();
  $("#gName").value = c.name || "";
  $$("[data-field]", $("#gProfileFields")).forEach((inp) => {
    inp.value = (c.profile || {})[inp.dataset.field] || "";
  });
  $$("[data-conf]", $("#gProfileFields")).forEach((el) => {
    const level = (c.confidence || {})[el.dataset.conf] || "";
    el.textContent = level === "inferred" ? "推断" : level === "missing" ? "缺失" : "";
    el.className = "conf " + (level === "inferred" ? "inferred" : "");
  });
  $("#gAnchor").value = c.anchor || "";
  $("#gRefAsset").value = c.ref_asset_id || "";
  $("#gCardBadge").textContent = c.id
    ? `锚点 v${c.anchor_version || 1} · ${c.id}` : "未保存的新角色";
  const evidence = Object.entries(c.evidence || {}).slice(0, 4)
    .map(([k, v]) => `${esc(gameFieldLabel(k))}：${esc(v)}`).join("<br>");
  $("#gCardInfo").innerHTML =
    `<div class="kv"><span>锚点版本</span><span>v${esc(c.anchor_version || 1)}</span></div>` +
    `<div class="kv"><span>历史版本</span><span>${esc((c.anchor_history || []).length)} 个</span></div>` +
    `<div class="kv"><span>来源</span><span>${esc(c.mode || "manual")}</span></div>` +
    (evidence ? `<div class="section-label">原文证据（自动提取，请核对）</div><div>${evidence}</div>` : "");
}

function collectGameCard() {
  const c = JSON.parse(JSON.stringify(G.card || blankGameCard()));
  c.profile = c.profile || {};
  c.name = $("#gName").value.trim();
  $$("[data-field]", $("#gProfileFields")).forEach((inp) => {
    c.profile[inp.dataset.field] = inp.value.trim();
  });
  c.anchor = $("#gAnchor").value.trim();
  c.ref_asset_id = $("#gRefAsset").value;
  return c;
}

async function selectGameCard(id) {
  if (!id) { G.card = blankGameCard(); renderGameCard(); return; }
  G.card = await api("/api/characters/" + encodeURIComponent(id));
  renderGameCard();
  await gamePromptPreview(true);
}

async function saveGameCard() {
  const card = collectGameCard();
  if (!card.name) { toast("请先填写角色名", "err"); return; }
  const saved = await api("/api/characters", { method: "POST", body: card });
  toast(`已保存 ${saved.name} · 锚点 v${saved.anchor_version}`, "ok");
  await loadGameCharacters(saved.id);
  await refreshAll();
}

function newGameCard() {
  G.card = blankGameCard();
  $("#gCardSelect").value = "";
  renderGameCard();
  toast("已切换到空白角色卡", "");
}

function deleteGameCard() {
  const c = G.card || {};
  if (!c.id) { toast("这是未保存的空白角色，无需删除", ""); return; }
  confirmModal("删除角色卡", `确定删除 <b>${esc(c.name)}</b> 的角色卡吗？已生成的图片和资产包不会被删除，但会失去角色关联。`,
    async () => {
      await api("/api/characters/" + encodeURIComponent(c.id), { method: "DELETE" });
      G.card = blankGameCard();
      await loadGameCharacters("");
      toast("角色卡已删除", "ok");
    });
}

async function gameExtract() {
  const text = $("#gNovel").value.trim();
  if (text.length < 20) { toast("请粘贴至少一段小说正文（20 字以上）", "err"); return; }
  const btn = document.querySelector('[data-action="g-extract"]');
  btn.disabled = true; btn.textContent = "提取中…";
  try {
    const res = await api("/api/characters/extract", {
      method: "POST", body: { text, mode: $("#gExtractMode").value },
    });
    G.extracted = res.characters || [];
    $("#gExtractNote").textContent = res.note || "";
    renderExtractResults();
    toast(`识别到 ${G.extracted.length} 个角色（${res.mode}）`, "ok");
  } finally {
    btn.disabled = false; btn.textContent = "提取人物特征";
  }
}

function renderExtractResults() {
  $("#gExtractResults").innerHTML = G.extracted.map((c, i) => {
    const traits = Object.entries(c.profile || {})
      .filter(([k, v]) => v && !["art_style", "negative"].includes(k))
      .slice(0, 6)
      .map(([k, v]) => `${esc(gameFieldLabel(k))}：${esc(v)}`).join("<br>");
    return `<div class="extract-card">
      <div class="head"><span class="name">${esc(c.name)}</span>
        <button class="btn ghost" data-action="g-load-extract" data-i="${i}">载入到角色卡</button></div>
      <div class="traits">${traits || "未提取到明确外貌特征，请在角色卡里手工补全。"}</div>
    </div>`;
  }).join("") || `<div class="muted small">没有识别到人物。可以点「新建空白角色」手工填写。</div>`;
}

function loadExtract(i) {
  const ex = G.extracted[Number(i)];
  if (!ex) return;
  const existing = (G.cards || []).find((c) => c.name === ex.name);
  const base = existing ? JSON.parse(JSON.stringify(existing)) : blankGameCard();
  base.name = ex.name;
  base.profile = Object.assign({}, base.profile, ex.profile || {});
  base.evidence = Object.assign({}, base.evidence, ex.evidence || {});
  base.confidence = Object.assign({}, base.confidence, ex.confidence || {});
  base.source = ex.source || "";
  base.mode = ex.mode || "manual";
  base.anchor = "";             // let the server rebuild it from the profile
  G.card = base;
  $("#gCardSelect").value = base.id || "";
  renderGameCard();
  toast(existing ? "已合并到同名角色卡，保存后会升级锚点版本" : "已载入，核对后点「保存角色卡」", "ok");
  $("#gCardSelect").scrollIntoView({ behavior: "smooth", block: "center" });
}

async function gamePromptPreview(silent) {
  const cid = (G.card || {}).id;
  const box = $("#gPromptPreview");
  if (!cid) {
    if (box) box.textContent = "先保存角色卡，才能预览带锚点的完整提示词。";
    if (!silent) toast("请先保存角色卡", "err");
    return;
  }
  const res = await api("/api/characters/" + encodeURIComponent(cid) + "/prompt");
  if (box) box.textContent = res.prompt || "";
  if (!silent) toast(`当前锚点 v${res.anchor_version}，已展开完整提示词`, "ok");
}

function requireGameCard() {
  if ((G.card || {}).id) return G.card.id;
  toast("请先选择并保存一张角色卡", "err");
  return "";
}

function renderGamePackResult(pack, el) {
  if (!pack) return;
  const d = pack;
  const detail = d.frames
    ? `${d.frames} 帧 · ${d.cell}×${d.cell} · ${d.fps} fps · sheet ${esc((d.sheet_size || []).join("×"))}`
    : `${(d.views || []).length} 个视图 · ${esc(d.mode || "")}`;
  el.innerHTML = `<div class="result-box ${d.demo ? "warn" : ""}">
    <div><b>${esc(d.character_name || d.name || d.id)}</b> · <span class="small">${esc(d.id)}</span></div>
    <div>${detail}</div>
    <div>${esc(d.note || "")}</div>
    <div class="btn-row">
      <a class="btn ghost" href="${esc(d.preview || "#")}" target="_blank" rel="noopener">打开预览</a>
      <a class="btn ghost" href="${esc(d.download || "#")}">下载 ZIP</a>
    </div></div>`;
}

async function gameTurnaround() {
  const cid = requireGameCard();
  if (!cid) return;
  const body = {
    mode: $("#gTurnMode").value, dry_run: $("#gDry").checked,
    ref_asset_id: $("#gRefAsset").value, model: $("#gModel").value,
    quality: $("#gQuality").value, name: $("#gPackName").value.trim(),
  };
  const unit = priceOf(body.model, body.quality);
  const n = body.mode === "separate" ? 4 : 1;
  const go = async () => {
    const res = await api("/api/characters/" + encodeURIComponent(cid) + "/turnaround",
      { method: "POST", body });
    renderGamePackResult(res.pack, $("#gTurnResult"));
    toast(res.pack.demo ? "已产出演示四视图包（未调用 API）" : "四视图包已生成", "ok");
    await loadGamePacks();
    await refreshAll();
  };
  if (body.dry_run) await go();
  else confirmModal("确认真实生成",
    `将调用 API 生成 <b>${n}</b> 张图，预估 <b>$${(unit * n).toFixed(4)}</b>。<br><br>生成期间页面会等待响应，请勿重复点击。`,
    go);
}

async function gameReproduce() {
  const cid = requireGameCard();
  if (!cid) return;
  const body = {
    dry_run: $("#gDry").checked, extra: $("#gExtra").value.trim(),
    ref_asset_id: $("#gRefAsset").value, model: $("#gModel").value,
    quality: $("#gQuality").value,
  };
  const unit = priceOf(body.model, body.quality);
  const go = async () => {
    const res = await api("/api/characters/" + encodeURIComponent(cid) + "/reproduce",
      { method: "POST", body });
    const a = res.asset || {};
    $("#gTurnResult").innerHTML = `<div class="result-box ${a.demo ? "warn" : ""}">
      <div><b>角色复现图已生成</b> · ${esc(a.id || "")}</div>
      <div>${res.reference_used ? "使用了参考图（input_fidelity=high）" : "仅使用角色锚点"}${res.degraded ? " · " + esc(res.degraded) : ""}</div>
      <div class="btn-row"><a class="btn ghost" href="/media/${esc(a.file || "")}" target="_blank" rel="noopener">查看图片</a></div>
    </div>`;
    toast(a.demo ? "已生成 Dry run 占位图" : "角色复现图已生成", "ok");
    await refreshAll();
  };
  if (body.dry_run) await go();
  else confirmModal("确认真实生成",
    `将调用 API 生成 1 张，预估 <b>$${unit.toFixed(4)}</b>。`, go);
}

async function gameSprite() {
  const assetId = $("#gSpriteAsset").value;
  if (!assetId) { toast("请先选择一张 PNG 基准图", "err"); return; }
  const body = {
    asset_id: assetId,
    actions: $$('#gActions input:checked').map((i) => i.value),
    engines: $$('#gEngines input:checked').map((i) => i.value),
    frames_per_action: Number($("#gFrames").value),
    cell: Number($("#gCell").value), cols: Number($("#gCols").value),
    fps: Number($("#gFps").value), bg: $("#gBg").value,
    tolerance: Number($("#gTol").value), name: $("#gPackName").value.trim(),
  };
  if (!body.actions.length) { toast("至少选一个动作", "err"); return; }
  const btn = document.querySelector('[data-action="g-sprite"]');
  btn.disabled = true; btn.textContent = "正在切帧…";
  try {
    const res = await api("/api/sprites", { method: "POST", body });
    renderGamePackResult(res.pack, $("#gSpriteResult"));
    toast(`已生成 ${res.pack.frames} 帧精灵动画包`, "ok");
    await loadGamePacks();
  } finally {
    btn.disabled = false; btn.textContent = "生成精灵动画包";
  }
}

async function loadGamePacks() {
  const res = await api("/api/packs");
  G.packs = res.packs || [];
  renderGamePacks();
}

function renderGamePacks() {
  $("#gPacks").innerHTML = G.packs.map((p) => {
    const d = p.data || {};
    const img = d.sheet || d.turnaround || "";
    const detail = d.frames
      ? `${d.frames} 帧 · ${d.cell}px · ${(d.actions || []).join("/")}`
      : `${(d.views || []).length} 视图 · ${esc(d.mode || "")}`;
    return `<div class="pack-row">
      ${img ? `<img class="pack-thumb" src="${esc(img)}" alt="">` : `<div class="pack-thumb"></div>`}
      <div class="pack-info">
        <div class="pack-name">${esc(p.name || p.id)} <span class="muted small">${esc(p.kind)}</span></div>
        <div class="pack-meta">${esc(p.created_at)} · ${d.demo ? "演示包" : "真实生成"} · ${esc(detail)}</div>
      </div>
      <div class="pack-actions">
        <a class="btn ghost" href="${esc(d.preview || "#")}" target="_blank" rel="noopener">预览</a>
        <a class="btn ghost" href="${esc(d.download || "#")}">下载</a>
        <button class="btn ghost" data-action="g-delete-pack" data-id="${esc(p.id)}">删除</button>
      </div></div>`;
  }).join("") || `<div class="muted small">还没有资产包。先用 Dry run 生成一个演示包看看结构。</div>`;
}

function deleteGamePack(pid) {
  confirmModal("删除资产包", `确定删除 <b>${esc(pid)}</b> 吗？包目录、逐帧 PNG 和 ZIP 都会一并删除，无法恢复。`,
    async () => {
      await api("/api/packs/" + encodeURIComponent(pid), { method: "DELETE" });
      await loadGamePacks();
      toast("资产包已删除", "ok");
    });
}

async function initGame() {
  if (!G.meta) await prepareGame();
  buildGameUI();
  await Promise.all([loadGameCharacters(), refreshGameAssets(), loadGamePacks()]);
  refreshGameEstimate();
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
    await prepareGame();
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
      case "refresh": await loadAssets(true); break;
      case "load-more": await loadAssets(false); break;
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
      case "g-extract": await gameExtract(); break;
      case "g-load-extract": loadExtract(el.dataset.i); break;
      case "g-save-card": await saveGameCard(); break;
      case "g-new-card": newGameCard(); break;
      case "g-delete-card": deleteGameCard(); break;
      case "g-turnaround": await gameTurnaround(); break;
      case "g-reproduce": await gameReproduce(); break;
      case "g-prompt": await gamePromptPreview(false); break;
      case "g-sprite": await gameSprite(); break;
      case "g-refresh-packs": await loadGamePacks(); break;
      case "g-delete-pack": deleteGamePack(el.dataset.id); break;
      case "do-export": await doExport(); break;
      case "do-sync": await doSync(); break;
      case "refresh-health": await loadHealth(); break;
      case "save-session-key": await saveSessionKey(); break;
      case "persist-key": await persistCurrentKey(); break;
      case "clear-persisted-key": await clearPersistedKey(); break;
      case "save-file-key": await saveFileKey(); break;
      case "clear-file-key": await clearFileKey(); break;
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

document.addEventListener("change", async (e) => {
  const id = e.target.id;
  if (e.target.dataset && e.target.dataset.opt) {
    e.target.closest(".opt").classList.toggle("on", e.target.checked);
  }
  if (id === "fCharacter") { await loadAssets(true); return; }
  if (id === "gCardSelect") { await selectGameCard(e.target.value); return; }
  if (id === "gModel") { refreshGameQuality(); return; }
  if (["gQuality", "gTurnMode", "gDry"].includes(id)) { refreshGameEstimate(); return; }
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

// If a thumbnail cannot be produced (unsupported PNG variant, SVG demo, etc.),
// fall back to the original image without breaking the card.
document.addEventListener("error", (e) => {
  const img = e.target;
  if (img && img.tagName === "IMG" && img.dataset.fallback) {
    const rel = img.dataset.fallback;
    delete img.dataset.fallback;
    img.src = "/media/" + rel;
  }
}, true);

function setupInfiniteScroll() {
  const el = $("#loadMore");
  if (!el || typeof IntersectionObserver === "undefined") return;
  new IntersectionObserver((entries) => {
    if (!entries[0].isIntersecting) return;
    if (S.view !== "gallery" || !S.assetsHasMore || S.assetsLoading) return;
    loadAssets(false);
  }, { rootMargin: "240px" }).observe(el);
}

boot();
setupInfiniteScroll();
