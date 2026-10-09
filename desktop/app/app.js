/* 我的产品桌面版 D1.0 —— 全部渲染在本机，翻页/筛选零延迟 */
"use strict";

const S = {
  profile: null, version: "",
  items: [], cats: [], owners: [],
  sel: new Set(),
  page: 0, per: 20,
  status: "", date: "all", cat: "", q: "", sort: "updated",
  scope: "",
  filtered: null,
  pollTimer: null, taskOpen: false,
  /* D1.3.0 智赢式分类 */
  view: "list",            // list / cat / recycle
  lib: null,               // {nodes:[{name,parent}], deleted:[{path,at}], rev}
  viewCat: "",             // 分类视图当前选中（""=全部，"__none__"=未分类）
  expanded: null,          // Set 展开路径（localStorage 记忆）
  treeQ: "",               // 分类搜索
  moveCtx: null,           // 移动分类弹窗上下文
  catEditCtx: null,        // 新增/修改分类上下文
  catDelCtx: null,         // 删除/彻底删除上下文
  /* D1.5.0 刊登词典（智赢式） */
  words: [],               // [{bad,fix,at,by}] 侵权词对照表（全局共享）
  ptmap: [],               // [{cat,pt,at,by}] 分类→亚马逊商品类型
  btmap: [],               // [{cat,node,name,at,by}] 分类→亚马逊分类节点
};

/* D = 当前详情弹窗的图片编辑状态；IE = 图片编辑器状态；AZ = 亚马逊上传 */
const D = { pid: "", imgs: [], dirty: false, thin: false };
const IE = { work: null, init: null, idx: -1, isNew: false, zoom: 1, mode: "view" };
const AZ = { poll: null, authPoll: null, status: null, up: {} };
const SCAN = { hits: null };   // D1.5.0 侵权扫描结果

const $ = (id) => document.getElementById(id);

/* ---------- 工具 ---------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtMs(ms) {
  const n = Number(ms) || 0;
  if (!n) return "";
  const d = new Date(n);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function toast(msg, type = "", ms = 3800) {
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function busy(on, text = "处理中…") {
  $("busyText").textContent = text;
  $("busy").classList.toggle("hidden", !on);
}

async function api(path, body, opts = {}) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }

  if (res.status === 401 && !path.startsWith("/api/login")) {
    showLogin();
    throw new Error(data.error || "请重新登录");
  }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `请求失败（${res.status}）`);
  }
  return data;
}

/* ---------- 启动 / 登录 ---------- */

async function boot() {
  try {
    const s = await api("/api/session");
    S.version = s.version || "D1.0";
    $("verPill").textContent = S.version;

    if (s.logged_in) {
      S.profile = s.profile;
      enterApp();
    } else {
      showLogin();
    }
  } catch (e) {
    showLogin();
    toast("本地服务异常：" + e.message, "err");
  }
}

function showLogin() {
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  stopPoll();
}

async function doLogin() {
  const user = $("loginUser").value.trim();
  const pass = $("loginPass").value;
  $("loginErr").textContent = "";
  $("loginBtn").disabled = true;

  try {
    const r = await api("/api/login", { user, pass });
    S.profile = r.profile;
    enterApp();
  } catch (e) {
    $("loginErr").textContent = e.message;
  }
  $("loginBtn").disabled = false;
}

function enterApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");

  const p = S.profile || {};
  $("userChip").textContent =
    p.user + (p.admin ? " · 管理员" : p.head ? " · 组长" : "");
  $("btnAi").classList.toggle("hidden", !p.admin);
  $("selScope").classList.toggle("hidden", !p.admin);
  S.sel = new Set(JSON.parse(localStorage.getItem("wz_sel") || "[]"));
  S.expanded = new Set(JSON.parse(localStorage.getItem("wz_tree_open") || "[]"));
  loadCats();                            // 分类树（全局共享，预置智赢 8 顶级）
  loadDict();                            // 刊登词典（侵权词 + 分类映射）
  loadProducts()                        // 磁盘缓存瞬间出列表
    .then(() => {
      const m = location.hash.match(/^#pid=(.+)$/);  // 深链/自测：#pid=xxx 直接开详情
      if (m) openDetail(decodeURIComponent(m[1]));
    });
  setTimeout(() => loadProducts(true), 1200);  // 随后拉最新
  pollTask();  // 万一上次任务还在跑
  pollAutoCat();  // AI 归类同理（在跑就接上进度，空闲首轮即停）
}

async function doLogout() {
  try { await api("/api/logout", {}); } catch (e) { /* ignore */ }
  S.profile = null; S.items = [];
  showLogin();
}

/* ---------- 产品列表 ---------- */

async function loadProducts(force = false) {
  try {
    $("netDot").classList.remove("off");
    const r = await api(
      `/api/products?scope=${encodeURIComponent(S.scope)}${force ? "&force=1" : ""}`
    );
    S.items = r.items || [];
    S.cats = r.cats || [];
    S.owners = r.owners || [];
    $("netDot").classList.remove("off");

    const valid = new Set(S.items.map((it) => String(it.pid)));
    S.sel = new Set([...S.sel].filter((p) => valid.has(p)));
    saveSel();

    S.filtered = null;   // 页码保留（renderGrid 自动钳回；筛选变化才归零）
    renderAll();
  } catch (e) {
    $("netDot").classList.add("off");
    toast(e.message, "err");
  }
}

function saveSel() {
  localStorage.setItem("wz_sel", JSON.stringify([...S.sel]));
}

/* 变更后：第一遍读本地乐观缓存（刚改的立即生效），随后静默
  强拉两遍对齐云端真数据（KV 同步最长要 60 秒）。 */
async function loadProductsTwice() {
  await loadProducts();
  setTimeout(() => loadProducts(true), 6000);
  setTimeout(() => loadProducts(true), 20000);
}

function stateOf(it) {
  if (it.has_opt) return "opt";
  if (Number(it.n_rows) > 0) return "ready";
  return "thin";
}

const BADGE = {
  opt: ["✅ 已优化", "opt"], ready: ["⏳ 待优化", "ready"],
  thin: ["⚠️ 资料不全", "thin"],
};

function computeFiltered() {
  const now = Date.now();
  const days = { w1: 7, m1: 30, m3: 91, m6: 183, y1: 365, y3: 1096 };
  const lo = S.date in days
    ? now - days[S.date] * 86400000 : null;
  const needle = S.q.replace(/\s+/g, "").toLowerCase();

  /* 分类筛选：值是分类路径（"3C数码/手机壳"），选中父分类=含全部子孙；
     "__none__" = 未分类。列表视图用 selCat（S.cat），分类视图用树（S.viewCat） */
  const catSel = S.view === "cat" ? S.viewCat : S.cat;

  let list = S.items.filter((it) => {
    if (S.status && stateOf(it) !== S.status) return false;

    if (catSel === "__none__") {
      if (String(it.cat || "").trim()) return false;
    } else if (catSel) {
      const c = String(it.cat || "");
      if (c !== catSel && !c.startsWith(catSel + "/")) return false;
    }

    if (lo !== null) {
      const c = Number(it.created) || 0;
      if (!c || c < lo) return false;
    }

    if (needle) {
      const hay = (String(it.title || "") + String(it.sku || "")
        + String(it.model || "")).replace(/\s+/g, "").toLowerCase();
      if (!hay.includes(needle)) return false;
    }
    return true;
  });

  const keys = {
    updated: (x) => -(Number(x.updated) || 0),
    created: (x) => -(Number(x.created) || 0),
    created_asc: (x) => Number(x.created) || 0,
    opt: (x) => -(Number(x.opt_at) || 0),
  };
  list.sort((a, b) => (keys[S.sort] || keys.updated)(a) - (keys[S.sort] || keys.updated)(b));
  S.filtered = list;
}

function readOnly() { return S.scope === "all"; }

function renderAll() {
  computeFiltered();
  renderTabs();
  renderCatSelect();
  renderGrid();
  renderPager();
  renderSelBar();
  applyView();
  renderTree();
  renderCatHead();
  renderRecycle();
  renderWords();
}

function renderTabs() {
  const counts = { all: S.items.length, opt: 0, ready: 0, thin: 0 };
  S.items.forEach((it) => counts[stateOf(it)]++);

  const defs = [
    ["", `全部（${counts.all}）`],
    ["ready", `⏳ 待优化（${counts.ready}）`],
    ["opt", `✅ 已优化（${counts.opt}）`],
    ["thin", `⚠️ 资料不全（${counts.thin}）`],
  ];
  $("statusTabs").innerHTML = defs.map(([code, label]) =>
    `<button class="tab ${S.status === code ? "on" : ""}" data-st="${code}">${label}</button>`
  ).join("");

  const dates = [
    ["all", "全部时间"], ["w1", "一周内"], ["m1", "一月内"],
    ["m3", "三月内"], ["m6", "半年内"], ["y1", "一年内"], ["y3", "三年内"],
  ];
  $("dateTabs").innerHTML = dates.map(([code, label]) =>
    `<button class="tab small ${S.date === code ? "on" : ""}" data-dt="${code}">${label}</button>`
  ).join("");
}

function renderCatSelect() {
  /* 下拉也按树形缩进展示（智赢是级联，桌面窄下拉用全角空格缩进平替） */
  const rows = treeRows().rows;
  const cur = S.cat;
  const has = !cur || cur === "__none__" || rows.some((r) => r.path === cur);
  $("selCat").innerHTML =
    `<option value="">全部分类</option>` +
    `<option value="__none__" ${cur === "__none__" ? "selected" : ""}>未分类</option>` +
    rows.map((r) =>
      `<option value="${esc(r.path)}" ${cur === r.path ? "selected" : ""}>` +
      `${"　".repeat(r.depth)}${esc(r.name)}${r.total ? `（${r.total}）` : ""}</option>`
    ).join("") +
    (has ? "" : `<option value="${esc(cur)}" selected>${esc(cur)}</option>`);
}

function renderGrid() {
  const list = S.filtered || [];
  const pages = Math.max(1, Math.ceil(list.length / S.per));
  S.page = Math.min(S.page, pages - 1);

  const slice = list.slice(S.page * S.per, (S.page + 1) * S.per);
  const ro = readOnly();

  $("empty").classList.toggle("hidden", list.length > 0);
  $("grid").innerHTML = slice.map((it) => {
    const st = stateOf(it);
    const [label, cls] = BADGE[st];
    const img = String(it.img || "").trim();
    const on = S.sel.has(String(it.pid));
    const optMs = Number(it.opt_at) || 0;
    const srcSt = String(it.status || "");
    const meta = [
      `SKU：${esc(String(it.sku || "—").slice(0, 24))}　型号：${esc(String(it.model || "—").slice(0, 20))}`,
      `分类：${esc(String(it.cat || "未分类").slice(0, 44))}（资料 ${Number(it.n_rows) || 0} 行）`,
      optMs ? `优化时间：${fmtMs(optMs)}` : "",
      srcSt === "found" ? "✅ 已找到供应商" : srcSt === "none" ? "❌ 没找到供应商" : "",
      ro && it.owner ? `<span class="owner">👤 ${esc(String(it.owner).slice(0, 16))}</span>` : "",
    ].filter(Boolean).join("<br>");

    return `<div class="card ${on ? "sel" : ""}" data-pid="${esc(it.pid)}">
      <div class="imgbox">${img
        ? `<img loading="lazy" referrerpolicy="no-referrer" src="${esc(img)}"
             onerror="this.style.visibility='hidden'">`
        : "（无图片）"}</div>
      <span class="badge ${cls}">${label}</span>
      <div class="t" title="${esc(it.title)}">${esc(String(it.title || "（无标题）").slice(0, 60))}</div>
      <div class="meta">${meta}</div>
      <div class="acts">
        ${ro ? `<span class="hint">👁 点卡片看详情</span>` : `<button class="btn ${on ? "on" : ""}" data-act="sel">${on ? "☑ 已选中" : "☐ 选择"}</button>`}
      </div>
    </div>`;
  }).join("");
}

function renderPager() {
  const list = S.filtered || [];
  const pages = Math.max(1, Math.ceil(list.length / S.per));

  $("pageInfo").textContent =
    `第 ${S.page + 1} / ${pages} 页 · 共找到 ${list.length} 个（库里共 ${S.items.length} 个）`;
  $("btnPrev").disabled = S.page <= 0;
  $("btnNext").disabled = S.page >= pages - 1;
  $("inpJump").max = pages;
  if (+$("inpJump").value > pages) $("inpJump").value = S.page + 1;
}

function renderSelBar() {
  const ro = readOnly();
  ["btnSelAll", "btnSelNone", "btnOpt", "btnCat", "btnAutoCat", "btnDel",
   "btnExport", "btnAmz", "btnImportToggle"].forEach((id) =>
    $(id).classList.toggle("hidden", ro));

  if (ro) {
    $("selCount").textContent = "👁 只读视图 — 切回「我的库」才能操作";
    return;
  }
  $("selCount").textContent = `已选 ${S.sel.size} 个（跨页保留）`;
  $("btnOpt").disabled = S.sel.size === 0;
  $("btnCat").disabled = S.sel.size === 0;
  if (!acTimer) $("btnAutoCat").disabled = S.sel.size === 0;
  $("btnDel").disabled = S.sel.size === 0;
  $("btnExport").disabled = S.sel.size === 0;
  $("btnAmz").disabled = S.sel.size === 0;
}

/* ---------- 详情：点卡片立刻秒开（列表缓存先画壳，资料到了再填） ---------- */

function openDetail(pid) {
  pid = String(pid);
  const it = S.items.find((x) => String(x.pid) === pid) || { pid };
  renderDetailShell(it);
  $("dlgDetail").classList.remove("hidden");

  const owner = readOnly() ? String(it.owner || "") : "";
  api(`/api/product?pid=${encodeURIComponent(pid)}${owner ? `&owner=${encodeURIComponent(owner)}` : ""}`)
    .then((r) => fillDetail(r.product || {}, it))
    .catch((e) => {
      const el = $("dLoading");
      if (el) el.innerHTML = `<div class="warn">⚠️ 详细资料读取失败：${esc(e.message)}（基础信息还在，关掉重开一次即可）</div>`;
    });
}

function dEditRow(cat, status) {
  return `<div class="d-edit">
      <div>标记<select id="dStatus" class="inp">
        <option value="wait" ${status === "wait" ? "selected" : ""}>⚪ 待找货</option>
        <option value="found" ${status === "found" ? "selected" : ""}>✅ 已找到</option>
        <option value="none" ${status === "none" ? "selected" : ""}>❌ 没找到</option>
      </select></div>
      <div class="grow d-cat-pick">分类<input id="dCat" class="inp" value="${esc(cat || "")}" maxlength="120" placeholder="点 📂 从分类树选择" readonly>
        <button class="btn" onclick="openMove({ single: true, fill: 'dCat' })" title="从分类树选择">📂</button></div>
      <button id="dSave" class="btn primary">💾 保存标记</button>
      <button id="dOpt1" class="btn">🚀 优化这个产品</button>
      <button id="dScan" class="btn">🛡 查侵权</button>
    </div>`;
}

/* 图片加载失败兜底：直连不行 → 本机代理拉一遍 → 再不行藏起来（格子还在，可删） */
window.imgFail = function (el) {
  if (!el.dataset.p2 && el.dataset.u) {
    el.dataset.p2 = "1";
    el.src = "/api/image/proxy?u=" + encodeURIComponent(el.dataset.u);
  } else {
    el.style.visibility = "hidden";
  }
};

function dHead(img, title, sku, model, extra) {
  return `<div class="d-head">
    <div class="d-img-wrap">
      <div class="d-img" title="点图片看大图">${img
        ? `<img id="dMainImg" referrerpolicy="no-referrer" src="${esc(img)}" data-u="${esc(img)}"
               onerror="imgFail(this)" style="cursor:zoom-in">`
        : '<span id="dMainBox">（无图片）</span>'}</div>
    </div>
    <div class="d-info">
      <div class="line"><b>${esc(String(title || "（无标题）").slice(0, 90))}</b></div>
      <div class="line">SKU：<b>${esc(sku || "—")}</b>　型号：<b>${esc(model || "—")}</b></div>
      ${extra}
    </div>
  </div>`;
}

/* 秒开的壳：全部来自列表缓存，零网络请求 */
function renderDetailShell(it) {
  const st = stateOf(it);
  const extra =
    `<div class="line">分类：<b>${esc(it.cat || "未分类")}</b>　资料：<b>${Number(it.n_rows) || 0} 行</b>　状态：<b>${BADGE[st][0]}</b></div>`
    + (readOnly() && it.owner ? `<div class="line">👤 ${esc(String(it.owner).slice(0, 16))}</div>` : "");

  $("dTitle").textContent = "📋 产品详情";
  $("dBody").innerHTML =
    dHead(String(it.img || "").trim(), it.title, it.sku, it.model, extra)
    + (readOnly() ? "" : dEditRow(
        it.cat,
        ["wait", "found", "none"].includes(String(it.status)) ? String(it.status) : "wait",
      ))
    + `<div id="dLoading" class="d-sec"><div class="skel"></div><div class="skel" style="width:70%"></div><div class="skel" style="width:45%"></div></div>`;
  $("dBody").dataset.pid = String(it.pid || "");
  bindDetailActs();
}

/* 资料到了：完整重画（图片集 + AI 结果 + 资料明细 + 变体） */
function fillDetail(rec, it) {
  const opt = rec.opt || {};
  const raw = rec.raw || [];
  const ro = readOnly();
  const status = ["wait", "found", "none"].includes(String(rec.status)) ? String(rec.status) : "wait";
  const st = stateOf({ has_opt: !!(opt.title || (opt.bullets || []).length), n_rows: raw.length });

  // 图片集：产品图 + 简介图，去重（第一张 = 主图）
  const imgs = [];
  raw.forEach((r) => {
    (r.image_urls || []).concat(r.detail_image_urls || []).forEach((u) => {
      u = String(u || "").trim();
      if (u && !imgs.includes(u)) imgs.push(u.slice(0, 500));
    });
  });
  if (rec.img && !imgs.includes(String(rec.img).trim())) imgs.unshift(String(rec.img).trim().slice(0, 500));
  const mainImg = String(rec.img || opt.image || imgs[0] || "").trim();

  D.pid = String(rec.pid || it.pid || "");
  D.imgs = imgs.slice(0, 60);
  D.dirty = false;
  D.thin = !raw.length;
  D.rec = rec;
  D.it = it;

  const extra =
    `<div class="line">分类：<b>${esc(rec.cat || "未分类")}</b>　资料：<b>${raw.length} 行</b>　状态：<b>${BADGE[st][0]}</b></div>`
    + (opt.at ? `<div class="line">上次优化：${fmtMs(opt.at)}</div>` : "")
    + `<div class="line">导入：${fmtMs(rec.created)}　更新：${fmtMs(rec.updated)}</div>`
    + (ro && rec.owner ? `<div class="line">👤 ${esc(rec.owner)}</div>` : "");

  const parts = [dHead(mainImg, rec.title || it.title, rec.sku, it.model, extra)];

  if (!ro) parts.push(dEditRow(rec.cat, status));

  // 图片集（ERP 式：可删/可加/拖动排序/点图编辑；只读视图=缩略图条）
  const canEdit = !ro && !D.thin;
  parts.push(
    `<div class="d-sec"><h4>🖼 图片集（共 ${imgs.length} 张${canEdit ? "" : " · 只读"}）</h4>`
    + (canEdit
      ? `<div id="dSlots" class="d-slots"></div>
         <div class="d-imgbar">
           <button id="dImgSave" class="btn small primary" disabled>💾 保存图片修改</button>
           <button id="dImgDedup" class="btn small">🧹 清理重复</button>
           <button id="dImgCopy" class="btn small">🔗 复制主图链接</button>
           <button id="dImgClear" class="btn small danger">🗑 清空图片</button>
         </div>
         <div class="d-imghint">第 1 张=主图（白底 800×800 最佳）· 拖动格子调顺序 · 点图片=编辑（裁剪/旋转/白底）· ✕ 删除 · ⭐ 设为主图 · 改完点「💾 保存图片修改」</div>`
      : `<div class="d-thumbs">${imgs.slice(0, 24).map((u) =>
          `<img src="${esc(u)}" data-thumb="${esc(u)}" loading="lazy" referrerpolicy="no-referrer"
                onerror="imgFail(this)" data-u="${esc(u)}" title="点击看大图">`).join("")}</div>`
        + (D.thin
          ? `<div class="d-imghint">⚠️ 资料不全（只有主图）。用「📥 导入产品」传同一份 Excel 补全后，这里就能删/加/编辑图片。</div>`
          : `<div class="d-imghint">只读视图 — 切回「我的库」才能编辑图片。</div>`))
    + `</div>`
  );

  // AI 结果
  if (opt.title || (opt.bullets || []).length || opt.description) {
    const sec = [];
    if (opt.title) sec.push(`<div class="txt"><b>标题：</b>${esc(opt.title)}</div>`);
    if (opt.short_title) sec.push(`<div class="txt"><b>短标题：</b>${esc(opt.short_title)}</div>`);
    if ((opt.bullets || []).length)
      sec.push(`<div class="txt"><b>五点：</b><ul>${opt.bullets.map((b) => `<li>${esc(b)}</li>`).join("")}</ul></div>`);
    if ((opt.highlight || []).length)
      sec.push(`<div class="txt"><b>商品亮点：</b><ul>${opt.highlight.map((h) => `<li>${esc(h)}</li>`).join("")}</ul></div>`);
    if (opt.description) sec.push(`<div class="txt"><b>简介：</b>\n${esc(opt.description)}</div>`);
    if ((opt.seo || []).length)
      sec.push(`<div class="txt"><b>SEO 关键词：</b>${esc((opt.seo || []).slice(0, 10).join("、"))}</div>`);
    parts.push(`<div class="d-sec" id="dSecOpt"><h4>🤖 AI 优化结果${ro ? "" : ' <button id="dOptEdit" class="btn small">✏️ 编辑</button>'}</h4>${sec.join("")}</div>`);
  } else {
    parts.push(`<div class="d-sec" id="dSecOpt"><h4>🤖 AI 优化结果</h4><div class="txt" style="color:#999">还没优化过。点上面「🚀 优化这个产品」，跑完结果自动挂上来。</div></div>`);
  }

  // 资料明细（智赢式合并）：变体们的资料是一样的，公共部分只显示
  // 一份；各行值不同的列（通常是 SKU）单独列一张「变体 SKU」表。
  if (raw.length) {
    const keyOrder = [];
    raw.forEach((r) => {
      Object.keys(r.raw_data || {}).forEach((k) => {
        if (!keyOrder.includes(k)) keyOrder.push(k);
      });
    });
    const val = (r, k) => String((r.raw_data || {})[k] ?? "");
    const hasAny = (k) => raw.some((r) => val(r, k) !== "");
    const showable = (k) => k !== "产品图" && k !== "简介图" && hasAny(k);
    const isCommon = (k) => raw.every((r) => val(r, k) === val(raw[0], k));
    const commonKeys = keyOrder.filter((k) => showable(k) && isCommon(k));
    const varKeys = keyOrder.filter((k) => showable(k) && !isCommon(k));

    const cell = (k, v) =>
      k === "参考网址" && String(v).startsWith("http")
        ? `<a href="${esc(String(v))}" target="_blank" rel="noopener">${esc(String(v).slice(0, 60))}</a>`
        : esc(String(v));
    const commonTable = commonKeys.length
      ? `<table>${commonKeys.map((k) =>
          `<tr><td class="k">${esc(k)}</td><td>${cell(k, val(raw[0], k))}</td></tr>`).join("")}</table>`
      : `<div class="d-imghint">（除变体列外没有其他资料）</div>`;
    const multi = raw.length > 1;
    const varTable = multi && varKeys.length
      ? `<div class="d-sub">变体 SKU（${raw.length} 个，只有这些列各不相同）</div>
         <table class="vartbl"><tr><th style="width:34px">#</th>${varKeys.map((k) => `<th>${esc(k)}</th>`).join("")}</tr>`
        + raw.map((r, i) => `<tr><td class="k">${i + 1}</td>${
            varKeys.map((k) => `<td>${cell(k, val(r, k))}</td>`).join("")
          }</tr>`).join("")
        + `</table>
         <div class="d-sub">公共资料（${raw.length} 个变体共用这一份）</div>`
      : "";

    parts.push(`<div class="d-sec" id="dSecRaw"><h4>📋 资料明细${multi ? `（公共资料 1 份 + 变体 ${raw.length} 个 SKU）` : "（原始资料 1 行）"}${ro ? "" : ' <button id="dRawEdit" class="btn small">✏️ 编辑文字</button>'}</h4>`
      + (multi ? varTable + commonTable : commonTable)
      + `</div>`);
  } else {
    parts.push(`<div class="d-sec"><div class="warn">⚠️ 资料不全：这条产品没存完整资料。用「📥 导入产品」传同一份 Excel 即可补全（相同 SKU 自动合并，不会重复）。</div></div>`);
  }

  // 变体（兜底：只有资料不全、没有 raw 行时才显示这张表；
  // 正常产品的变体已并进上面「资料明细」的变体 SKU 表，不再重复）
  const vars = rec.variants || [];
  if (vars.length && !raw.length) {
    parts.push(`<div class="d-sec"><h4>变体（${vars.length} 个）</h4>
      <table><tr><th>SKU</th><th>属性</th><th>标题</th></tr>
      ${vars.map((v) => `<tr><td>${esc(v.sku || "")}</td><td>${esc(v.attr || "")}</td><td>${esc(String(v.title || "").slice(0, 40))}</td></tr>`).join("")}
      </table></div>`);
  }

  $("dTitle").textContent = "📋 产品详情";
  $("dBody").innerHTML = parts.join("");
  $("dBody").dataset.pid = String(rec.pid || it.pid || "");

  if (canEdit) renderSlots();   // 可编辑格子条（拖动/删除/编辑/添加）

  bindDetailActs();
}

/* ---------- V3（D1.2.0）：图片集格子 + 编辑器 ---------- */

function markDirty() {
  D.dirty = true;
  const b = $("dImgSave");
  if (b) b.disabled = false;
}

function syncMainImg() {
  const m = $("dMainImg");
  if (!m) return;
  const u = D.imgs[0] || "";
  if (u) {
    m.style.display = "";
    m.dataset.u = u;
    delete m.dataset.p2;
    m.src = u;
  } else {
    m.style.display = "none";
  }
}

function renderSlots() {
  const box = $("dSlots");
  if (!box) return;

  box.innerHTML = D.imgs.map((u, i) => `<div class="slot${i === 0 ? " ismain" : ""}" draggable="true" data-i="${i}"
      title="点一下=编辑这张 · ✕=删除 · ⭐=设为主图 · 拖动调顺序">
      <img src="${esc(u)}" loading="lazy" referrerpolicy="no-referrer" onerror="imgFail(this)" data-u="${esc(u)}">
      ${i === 0 ? '<span class="mb">主图</span>' : ""}
      <span class="star" data-main="${i}" title="${i === 0 ? "已是主图" : "设为主图"}">⭐</span>
      <span class="del" data-del="${i}" title="删除这张">✕</span>
    </div>`).join("")
    + `<div class="slot add" data-add="1" title="添加图片（网址 / 本地图）">📷<i>添加</i></div>`;

  box.ondragstart = (e) => {
    const s = e.target.closest(".slot[data-i]");
    if (!s) return;
    e.dataTransfer.setData("text/plain", s.dataset.i);
    e.dataTransfer.effectAllowed = "move";
  };
  box.ondragover = (e) => {
    e.preventDefault();
    const s = e.target.closest(".slot[data-i]");
    if (s) s.classList.add("dragover");
  };
  box.ondragleave = (e) => {
    const s = e.target.closest(".slot[data-i]");
    if (s) s.classList.remove("dragover");
  };
  box.ondrop = (e) => {
    e.preventDefault();
    const s = e.target.closest(".slot[data-i]");
    const from = +e.dataTransfer.getData("text/plain");
    if (s && !Number.isNaN(from)) moveImg(from, +s.dataset.i);
  };

  const save = $("dImgSave");
  if (save) save.disabled = !D.dirty;
  syncMainImg();
}

function delImg(i) {
  if (i < 0 || i >= D.imgs.length) return;
  D.imgs.splice(i, 1);
  markDirty();
  renderSlots();
}

function setMain(i) {
  if (i <= 0 || i >= D.imgs.length) return;
  const [u] = D.imgs.splice(i, 1);
  D.imgs.unshift(u);
  markDirty();
  renderSlots();
}

function moveImg(from, to) {
  if (from === to || from < 0 || to < 0 || from >= D.imgs.length || to >= D.imgs.length) return;
  const [u] = D.imgs.splice(from, 1);
  D.imgs.splice(to, 0, u);
  markDirty();
  renderSlots();
}

function dedupImgs() {
  const before = D.imgs.length;
  D.imgs = [...new Set(D.imgs)];
  if (D.imgs.length !== before) {
    markDirty();
    toast(`🧹 清理了 ${before - D.imgs.length} 张重复图片`);
  } else {
    toast("没有发现重复图片");
  }
  renderSlots();
}

function clearImgs() {
  if (!D.imgs.length) { toast("本来就没有图片"); return; }
  if (!confirm(`确定清空全部 ${D.imgs.length} 张图片？\n（点「💾 保存图片修改」后才真正生效）`)) return;
  D.imgs = [];
  markDirty();
  renderSlots();
}

async function copyImgLink() {
  const u = D.imgs[0] || "";
  if (!u) { toast("没有图片可复制", "err"); return; }
  try {
    await navigator.clipboard.writeText(u);
    toast("✅ 主图链接已复制", "ok");
  } catch (e) {
    toast(u, "", 9000);
  }
}

async function saveImages() {
  if (!D.dirty) { toast("没有要保存的修改"); return; }
  try {
    busy(true, "保存图片修改…");
    await api("/api/product/images", { pid: D.pid, imgs: D.imgs });
    D.dirty = false;
    const b = $("dImgSave");
    if (b) b.disabled = true;
    toast("✅ 图片修改已保存（列表缩略图稍后跟着变）", "ok");
    loadProductsTwice();
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

function openAddImg() {
  $("iaUrl").value = "";
  $("iaFile").value = "";
  $("dlgImgAdd").classList.remove("hidden");
}

function openLightbox(u) {
  if (!u) return;
  const im = $("lbImg");
  im.onerror = () => {
    im.onerror = null;
    im.src = "/api/image/proxy?u=" + encodeURIComponent(u);
  };
  im.src = u;
  $("dlgLightbox").classList.remove("hidden");
}

/* ---------- V3.1（D1.2.1）：AI 结果 / 原始资料 文字手改 ---------- */

async function refillDetail() {
  /* 保存/取消后重拉资料刷新整个详情（不闪壳） */
  const pid = D.pid;
  const it = D.it || S.items.find((x) => String(x.pid) === pid) || { pid };
  const owner = readOnly() ? String(it.owner || "") : "";
  const r = await api(`/api/product?pid=${encodeURIComponent(pid)}${owner ? `&owner=${encodeURIComponent(owner)}` : ""}`);
  fillDetail(r.product || {}, it);
}

let OE_B = [];   // 编辑中的五点列表（AI 结果）

function collectOE() {
  OE_B = [...document.querySelectorAll("#oeBullets [data-oe]")].map((el) => el.value);
}

function renderOeBullets() {
  const box = $("oeBullets");
  if (!box) return;
  box.innerHTML = OE_B.map((b, i) => `<div class="oe-b">
      <textarea class="inp" rows="2" maxlength="600" data-oe="${i}">${esc(b)}</textarea>
      <button class="btn small danger" data-oedel="${i}" title="删掉这一条">✕</button>
    </div>`).join("");
}

function renderOptEdit() {
  const sec = $("dSecOpt");
  if (!sec || !D.rec) return;
  const opt = D.rec.opt || {};
  OE_B = (opt.bullets || []).map(String);
  sec.innerHTML = `<h4>🤖 AI 优化结果（编辑中）</h4>
    <div class="d-editform">
      <label>标题</label>
      <textarea id="oeTitle" class="inp" rows="2" maxlength="600">${esc(opt.title || "")}</textarea>
      <label>短标题</label>
      <input id="oeShort" class="inp" maxlength="300" value="${esc(opt.short_title || "")}">
      <label>五点描述（每条一行，✕ 删掉）</label>
      <div id="oeBullets"></div>
      <button id="oeBAdd" class="btn small">➕ 加一条五点</button>
      <label>简介</label>
      <textarea id="oeDesc" class="inp" rows="6" maxlength="8000">${esc(opt.description || "")}</textarea>
      <div class="d-imgbar">
        <button id="oeSave" class="btn small primary">💾 保存修改</button>
        <button id="oeCancel" class="btn small">取消</button>
      </div>
      <div class="d-imghint">保存立即生效。注意：之后再点「🚀 优化这个产品」重新跑 AI，会把这些手改内容覆盖掉</div>
    </div>`;
  renderOeBullets();
}

async function saveOptEdit() {
  collectOE();
  const opt = {
    title: $("oeTitle").value.trim(),
    short_title: $("oeShort").value.trim(),
    bullets: OE_B.map((b) => b.trim()).filter(Boolean).slice(0, 8),
    description: $("oeDesc").value.trim(),
  };

  if (!opt.title && !opt.bullets.length && !opt.description) {
    toast("标题、五点、简介都空了，至少留一条内容", "err");
    return;
  }

  try {
    busy(true, "保存修改…");
    await api("/api/product/opt", { pid: D.pid, opt });
    toast("✅ 修改已保存", "ok");
    await refillDetail();
    loadProductsTwice();
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

function renderRawEdit() {
  const sec = $("dSecRaw");
  if (!sec || !D.rec) return;
  const raw = D.rec.raw || [];
  const r0 = raw[0] || {};
  const rd = r0.raw_data || {};
  const bl = Array.isArray(r0.bullets) && r0.bullets.length ? r0.bullets : ["", "", "", "", ""];
  const five = [0, 1, 2, 3, 4].map((k) => bl[k] || "");
  sec.innerHTML = `<h4>📋 资料明细（编辑公共资料，共 ${raw.length} 行）</h4>
    <div class="d-editform">
      <label>标题</label>
      <textarea id="reT" class="inp" rows="2" maxlength="600">${esc(String(rd["标题(必填)"] || r0.title || ""))}</textarea>
      <label>五点描述（要点 1-5）</label>
      ${five.map((b, k) => `<textarea id="reB${k}" class="inp" rows="2" maxlength="600" placeholder="要点${k + 1}">${esc(b)}</textarea>`).join("")}
      <label>简介</label>
      <textarea id="reD" class="inp" rows="5" maxlength="8000">${esc(String(rd["简介"] || r0.description || ""))}</textarea>
      <div class="d-imgbar">
        <button id="reSave" class="btn small primary">💾 保存资料</button>
        <button id="reCancel" class="btn small">取消</button>
      </div>
      <div class="d-imghint">变体的公共资料是同一份：保存后应用到全部 ${raw.length} 行（各变体自己的 SKU 不变）。这里改的是导入的原始资料（AI 优化的底稿）；已优化过的产品，导出时用的是「AI 优化结果」里的内容</div>
    </div>`;
}

async function saveRawEdit() {
  const raw = (D.rec && D.rec.raw) || [];
  const one = {
    title: (($("reT") || {}).value || "").trim(),
    bullets: [0, 1, 2, 3, 4].map((k) => ((($("reB" + k) || {}).value) || "").trim()),
    description: (($("reD") || {}).value || "").trim(),
  };
  const rows = raw.map((r, i) => ({ i, ...one }));

  try {
    busy(true, "保存资料…");
    await api("/api/product/text", { pid: D.pid, rows });
    toast("✅ 资料已保存", "ok");
    await refillDetail();
    loadProductsTwice();
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

/* ---------- 图片编辑器（裁剪 / 旋转 / 白底 / 上传） ---------- */

function loadImg(src) {
  return new Promise((ok, no) => {
    const im = new Image();
    im.onload = () => ok(im);
    im.onerror = no;
    im.src = src;
  });
}

/* 图片 → 画布（超过 2000px 压一压；透明底垫白，导出 jpg 才不发黑） */
function imgToCanvas(im) {
  const w = im.naturalWidth || im.width || 1;
  const h = im.naturalHeight || im.height || 1;
  const k = Math.min(1, 2000 / Math.max(w, h));
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(w * k));
  c.height = Math.max(1, Math.round(h * k));
  const x = c.getContext("2d");
  x.fillStyle = "#fff";
  x.fillRect(0, 0, c.width, c.height);
  x.drawImage(im, 0, 0, c.width, c.height);
  return c;
}

function copyCanvas(src) {
  const c = document.createElement("canvas");
  c.width = src.width;
  c.height = src.height;
  c.getContext("2d").drawImage(src, 0, 0);
  return c;
}

async function openImgEditor(i, isNew, dataUrl) {
  const u = isNew ? "" : D.imgs[i];
  if (!isNew && !u) return;
  const src = dataUrl || "/api/image/proxy?u=" + encodeURIComponent(u);
  busy(true, "读取图片…");
  try {
    const im = await loadImg(src);
    IE.work = imgToCanvas(im);
    IE.init = copyCanvas(IE.work);
    IE.idx = isNew ? -1 : i;
    IE.isNew = !!isNew;
    IE.zoom = 1;
    endCrop();
    $("ieTitle").textContent = isNew
      ? "🖼 新图片（编辑好点「✅ 用这张」）"
      : `🖼 编辑第 ${i + 1} 张图片`;
    $("dlgImgEdit").classList.remove("hidden");
    requestAnimationFrame(draw);
  } catch (e) {
    toast("图片读取失败（网络原因）。可先把图下载到电脑，再用「📷 添加 → 本地图」", "err", 7000);
  } finally {
    busy(false);
  }
}

function draw() {
  if (!IE.work) return;
  const stage = $("ieStage");
  const cv = $("ieCanvas");
  const w = IE.work.width;
  const h = IE.work.height;
  const maxW = Math.max(220, stage.clientWidth - 26);
  const fit = Math.min(maxW / w, 430 / h, 1.5) * IE.zoom;
  const dw = Math.max(1, Math.round(w * fit));
  const dh = Math.max(1, Math.round(h * fit));
  cv.width = dw;
  cv.height = dh;
  const x = cv.getContext("2d");
  x.imageSmoothingEnabled = true;
  x.imageSmoothingQuality = "high";
  x.drawImage(IE.work, 0, 0, dw, dh);
  $("ieInfo").textContent = `当前 ${w}×${h} 像素 · 显示 ${Math.round(fit * 100)}%（滚轮缩放）`;
}

function rotateWork(dir) {
  if (!IE.work) return;
  const w = IE.work;
  const c = document.createElement("canvas");
  c.width = w.height;
  c.height = w.width;
  const x = c.getContext("2d");
  if (dir > 0) { x.translate(c.width, 0); x.rotate(Math.PI / 2); }
  else { x.translate(0, c.height); x.rotate(-Math.PI / 2); }
  x.drawImage(w, 0, 0);
  IE.work = c;
  IE.zoom = 1;
  draw();
}

/* 主图白底：等比缩放进 800×800 白底画布（第 1 张主图的标准格式） */
function whiteMain() {
  if (!IE.work) return;
  const c = document.createElement("canvas");
  c.width = 800;
  c.height = 800;
  const x = c.getContext("2d");
  x.fillStyle = "#fff";
  x.fillRect(0, 0, 800, 800);
  const w = IE.work;
  const k = Math.min(800 / w.width, 800 / w.height);
  const dw = w.width * k;
  const dh = w.height * k;
  x.drawImage(w, (800 - dw) / 2, (800 - dh) / 2, dw, dh);
  IE.work = c;
  IE.zoom = 1;
  draw();
  toast("已生成 800×800 白底主图（点「✅ 用这张」替换）");
}

function startCrop() {
  if (!IE.work) return;
  IE.mode = "crop";
  $("ieCropBar").classList.remove("hidden");
  $("ieStage").classList.add("cropping");
  $("ieCropBox").classList.add("hidden");
  $("ieCropSize").textContent = "按住鼠标拖出要保留的部分";
}

function endCrop() {
  IE.mode = "view";
  $("ieCropBar").classList.add("hidden");
  $("ieCropBox").classList.add("hidden");
  $("ieStage").classList.remove("cropping");
}

function applyCrop() {
  if (!IE.work) return;
  const box = $("ieCropBox");
  const stage = $("ieStage");
  const cv = $("ieCanvas");
  if (box.classList.contains("hidden")) { toast("先在图上拖出要保留的区域", "err"); return; }
  const sr = stage.getBoundingClientRect();
  const cr = cv.getBoundingClientRect();
  const bx = parseFloat(box.style.left) - (cr.left - sr.left);
  const by = parseFloat(box.style.top) - (cr.top - sr.top);
  const bw = parseFloat(box.style.width);
  const bh = parseFloat(box.style.height);
  const kx = IE.work.width / cr.width;
  const ky = IE.work.height / cr.height;
  const sx = Math.max(0, Math.round(bx * kx));
  const sy = Math.max(0, Math.round(by * ky));
  const sw = Math.min(IE.work.width - sx, Math.round(bw * kx));
  const sh = Math.min(IE.work.height - sy, Math.round(bh * ky));
  if (sw < 8 || sh < 8) { toast("选区太小了", "err"); return; }
  const c = document.createElement("canvas");
  c.width = sw;
  c.height = sh;
  c.getContext("2d").drawImage(IE.work, sx, sy, sw, sh, 0, 0, sw, sh);
  IE.work = c;
  IE.zoom = 1;
  endCrop();
  draw();
}

async function useEdited() {
  if (!IE.work) return;
  try {
    busy(true, "上传图片…");
    const b64 = IE.work.toDataURL("image/jpeg", 0.92).split(",")[1] || "";
    const r = await api("/api/image/put", { pid: D.pid, ct: "image/jpeg", data_b64: b64 });
    const url = String(r.url || "");
    if (!url) throw new Error("服务器没返回图片链接");
    if (IE.isNew) D.imgs.push(url);
    else D.imgs[IE.idx] = url;
    markDirty();
    closeModal("dlgImgEdit");
    renderSlots();
    toast(IE.isNew ? "✅ 已加入图片集" : "✅ 已替换这张图", "ok");
    toast("记得点「💾 保存图片修改」才真正保存", "", 6000);
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

function bindDetailActs() {
  $("dBody").onclick = (e) => {
    const del = e.target.closest("[data-del]");
    if (del) { delImg(+del.dataset.del); return; }

    const star = e.target.closest("[data-main]");
    if (star) { setMain(+star.dataset.main); return; }

    const add = e.target.closest("[data-add]");
    if (add) { openAddImg(); return; }

    const slot = e.target.closest(".slot[data-i]");
    if (slot) { openImgEditor(+slot.dataset.i); return; }

    if (e.target.closest("#dImgSave")) { saveImages(); return; }
    if (e.target.closest("#dImgDedup")) { dedupImgs(); return; }
    if (e.target.closest("#dImgCopy")) { copyImgLink(); return; }
    if (e.target.closest("#dImgClear")) { clearImgs(); return; }

    if (e.target.closest("#dOptEdit")) { renderOptEdit(); return; }
    if (e.target.closest("#dRawEdit")) { renderRawEdit(); return; }
    if (e.target.closest("#oeBAdd")) { collectOE(); OE_B.push(""); renderOeBullets(); return; }
    const odel = e.target.closest("[data-oedel]");
    if (odel) { collectOE(); OE_B.splice(+odel.dataset.oedel, 1); renderOeBullets(); return; }
    if (e.target.closest("#oeSave")) { saveOptEdit(); return; }
    if (e.target.closest("#oeCancel")) { refillDetail().catch(() => {}); return; }
    if (e.target.closest("#reSave")) { saveRawEdit(); return; }
    if (e.target.closest("#reCancel")) { refillDetail().catch(() => {}); return; }

    if (e.target.id === "dMainImg") { openLightbox(D.imgs[0] || e.target.dataset.u || ""); return; }

    const th = e.target.closest("[data-thumb]");
    if (th && $("dMainImg")) $("dMainImg").src = th.dataset.thumb;
  };
  const save = $("dSave");
  if (save) save.onclick = saveDetailMark;
  const opt1 = $("dOpt1");
  if (opt1) opt1.onclick = () => {
    const pid = $("dBody").dataset.pid;
    S.sel = new Set([pid]);
    saveSel();
    closeModal("dlgDetail");
    startOptimize();
  };
  const scan = $("dScan");
  if (scan) scan.onclick = openScan;
}

async function saveDetailMark() {
  const pid = $("dBody").dataset.pid;

  try {
    busy(true, "保存…");
    await api("/api/products/update", {
      updates: [{ pid, cat: $("dCat").value.trim(), status: $("dStatus").value }],
    });
    await loadProductsTwice();
    closeModal("dlgDetail");
    toast("✅ 已保存标记", "ok");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    busy(false);
  }
}

/* ---------- 分类树（D1.3.0 智赢式，严格对齐智赢交互） ----------
   数据：S.lib = {nodes:[{name,parent}], deleted, rev}（全局共享，worker 存）；
   产品的 cat 存完整路径（"3C数码/手机壳"）。树 = 文档定义 ∪ 产品里
   实际出现的路径（老数据的单名字自动成为顶级分类，零迁移）。 */

async function loadCats() {
  try {
    const r = await api("/api/cats");
    S.lib = r.lib || { nodes: [], deleted: [], rev: 0 };
    if (S.items.length || S.view !== "list") renderAll();
  } catch (e) { /* 树拉不到不挡产品列表 */ }
}

async function saveLib(mut) {
  const lib = {
    nodes: (S.lib?.nodes || []).map((n) => ({ name: n.name, parent: n.parent })),
    deleted: (S.lib?.deleted || []).map((d) => ({ path: d.path, at: d.at })),
    rev: S.lib?.rev || 0,
  };
  mut(lib);
  const r = await api("/api/cats", { lib });
  S.lib = { nodes: lib.nodes, deleted: lib.deleted, rev: r.rev || lib.rev + 1 };
  renderAll();
}

function buildTree() {
  const root = { name: "", path: "", parent: "", children: new Map(), depth: -1 };
  const ensure = (path) => {
    if (!path) return root;
    let node = root, p = "";
    for (const seg of String(path).split("/")) {
      p = p ? p + "/" + seg : seg;
      if (!node.children.has(seg)) {
        node.children.set(seg, {
          name: seg, path: p, parent: node.path, children: new Map(), depth: node.depth + 1,
        });
      }
      node = node.children.get(seg);
    }
    return node;
  };
  (S.lib?.nodes || []).forEach((n) => {
    if (n && n.name) ensure(n.parent ? n.parent + "/" + n.name : n.name);
  });
  S.items.forEach((it) => {
    const c = String(it.cat || "").trim();
    if (c && !c.startsWith("/")) ensure(c);
  });
  return root;
}

/* 树的扁平行序列（渲染/计数/下拉共用） */
function treeRows() {
  const root = buildTree();
  const exact = new Map();          // 路径 → 本类精确产品数
  let none = 0;
  S.items.forEach((it) => {
    const c = String(it.cat || "").trim();
    if (c) exact.set(c, (exact.get(c) || 0) + 1);
    else none++;
  });

  const rows = [];
  const walk = (node) => {
    const kids = [...node.children.values()];
    kids.sort((a, b) => a.name.localeCompare(b.name, "zh-Hans-CN"));
    kids.forEach((k) => {
      let total = exact.get(k.path) || 0;
      rows.push(k);
      walk(k);
      // total 回填：父 = 自己 + 子孙（利用 rows 顺序回头算太绕，递归返回）
      total += k._sub || 0;
      k._total = total;
      node._sub = (node._sub || 0) + total;
    });
  };
  walk(root);
  root._sub = root._sub || 0;
  return { rows, root, exact, none };
}

function catLabel(path) {
  return path === "__none__" ? "未分类" : (path || "全部产品");
}

/* 渲染树。target "#catTree"（管理树）或 "#moveTree"（弹窗单选树） */
function renderTree(target = "#catTree") {
  const box = $(target.slice(1));
  if (!box) return;
  const picker = target === "#moveTree";
  const { rows, exact, none } = treeRows();
  const q = S.treeQ.trim().toLowerCase();

  const matchSet = new Set();        // 搜索命中 + 祖先链
  if (q) {
    rows.forEach((r) => {
      if (r.name.toLowerCase().includes(q) || r.path.toLowerCase().includes(q)) {
        let p = r.path;
        while (p) { matchSet.add(p); const i = p.lastIndexOf("/"); p = i < 0 ? "" : p.slice(0, i); }
        matchSet.add(r.path);
        // 命中节点的子孙也显示
        rows.forEach((r2) => { if (r2.path.startsWith(r.path + "/")) matchSet.add(r2.path); });
      }
    });
  }
  const visible = (r) => !q || matchSet.has(r.path);

  const special = picker
    ? `<div class="tree-node sp ${S.moveCtx?.sel === "__none__" ? "picked" : ""}" data-pick="__none__">
         <span class="tw"></span><span class="tn">未分类</span><span class="tc">${none}</span></div>`
    : `<div class="tree-node sp ${S.viewCat === "" ? "on" : ""}" data-zpath="">
         <span class="tw"></span><span class="tn">全部产品</span><span class="tc">${S.items.length}</span></div>
       <div class="tree-node sp ${S.viewCat === "__none__" ? "on" : ""}" data-zpath="__none__">
         <span class="tw"></span><span class="tn">未分类</span><span class="tc">${none}</span></div>`;

  const nodeHtml = (r) => {
    const hasKids = r.children.size > 0;
    const isOpen = picker || q ? true : S.expanded.has(r.path);
    const cnt = exact.get(r.path) || 0;
    return `<div class="tree-node ${!picker && S.viewCat === r.path ? "on" : ""} ${picker && S.moveCtx?.sel === r.path ? "picked" : ""}"
        data-zpath="${esc(r.path)}" data-depth="${r.depth}" style="padding-left:${8 + r.depth * 16}px">
        <span class="tw ${hasKids ? "" : "leaf"}" data-toggle="${esc(r.path)}">${isOpen ? "▾" : "▸"}</span>
        <span class="tn" title="${esc(r.path)}">${esc(r.name)}</span>
        <span class="tc">${cnt || ""}</span>
        ${picker ? "" : `<span class="ti">
          <button class="tib" data-tact="ren" title="修改名称">✏️</button>
          <button class="tib" data-tact="add" title="新增子分类">📁+</button>
          <button class="tib" data-tact="del" title="删除分类">🗑</button>
        </span>`}
      </div>`;
  };

  /* 深度优先渲染，父收起时子孙不输出 */
  const out = [];
  const emit = (r) => {
    if (!visible(r)) return;
    out.push(nodeHtml(r));
    const isOpen = q || S.expanded.has(r.path);
    if (isOpen) [...r.children.values()]
      .sort((a, b) => a.name.localeCompare(b.name, "zh-Hans-CN"))
      .forEach(emit);
  };
  if (!picker) {
    const root = buildTree();
    [...root.children.values()]
      .sort((a, b) => a.name.localeCompare(b.name, "zh-Hans-CN"))
      .forEach(emit);
  } else {
    rows.forEach((r) => {
      /* 弹窗树：默认全展开（智赢弹窗就是全展开可滚） */
      const hasKids = r.children.size > 0;
      out.push(nodeHtml(r));
    });
  }

  box.innerHTML = special + out.join("");
}

/* ---------- 分类 CRUD（对齐智赢：悬停图标 → 弹窗 → 确定） ---------- */

function openCatEdit(ctx) {   // {mode:"add", parent} | {mode:"ren", path, name}
  S.catEditCtx = ctx;
  $("catEditTitle").textContent = ctx.mode === "add" ? "新增分类" : "修改分类";
  $("ceParent").value = ctx.mode === "add"
    ? (ctx.parent ? catLabel(ctx.parent) : "（顶级分类）")
    : (ctx.path.includes("/") ? ctx.path.slice(0, ctx.path.lastIndexOf("/")) : "（顶级分类）");
  $("ceName").value = ctx.mode === "ren" ? ctx.name : "";
  $("dlgCatEdit").classList.remove("hidden");
  setTimeout(() => $("ceName").focus(), 50);
}

async function catEditOk() {
  const ctx = S.catEditCtx;
  const name = $("ceName").value.trim();
  if (!ctx) return;
  if (!name) { toast("先填分类名称", "err"); return; }
  if (name.includes("/")) { toast("分类名称里不能有 /", "err"); return; }

  try {
    if (ctx.mode === "add") {
      const parent = ctx.parent || "";
      if ((S.lib?.nodes || []).some((n) => n.parent === parent && n.name === name)) {
        toast("这个分类已经存在了", "err"); return;
      }
      busy(true, "新增分类…");
      await saveLib((lib) => lib.nodes.push({ name, parent }));
      if (parent) S.expanded.add(parent);
      localStorage.setItem("wz_tree_open", JSON.stringify([...S.expanded]));
      toast(`✅ 已新增分类：${parent ? parent + "/" : ""}${name}`, "ok");
    } else {
      /* 改名：文档节点 + 产品 cat 前缀一起改 */
      const oldPath = ctx.path;
      const slash = oldPath.lastIndexOf("/");
      const parent = slash < 0 ? "" : oldPath.slice(0, slash);
      if ((S.lib?.nodes || []).some((n) => n.parent === parent && n.name === name)) {
        toast("同级已经有同名分类了", "err"); return;
      }
      const newPath = parent ? parent + "/" + name : name;
      const rePath = (p) => p === oldPath ? newPath
        : p.startsWith(oldPath + "/") ? newPath + p.slice(oldPath.length) : p;
      busy(true, "修改分类（含分类下的产品）…");
      const updates = S.items
        .filter((it) => {
          const c = String(it.cat || "");
          return c === oldPath || c.startsWith(oldPath + "/");
        })
        .map((it) => ({ pid: it.pid, cat: rePath(String(it.cat)) }));
      if (updates.length) {
        await api("/api/products/update", { updates });
      }
      /* 文档树重建：每个节点的完整路径过一遍 rePath 再拆回 name/parent */
      await saveLib((lib) => {
        lib.nodes = (S.lib?.nodes || []).map((n) => {
          const full = rePath(n.parent ? n.parent + "/" + n.name : n.name);
          const i = full.lastIndexOf("/");
          return i < 0 ? { name: full, parent: "" }
            : { name: full.slice(i + 1), parent: full.slice(0, i) };
        });
      });
      S.expanded.add(newPath);
      localStorage.setItem("wz_tree_open", JSON.stringify([...S.expanded]));
      if (S.cat === oldPath) S.cat = newPath;
      if (S.viewCat === oldPath) S.viewCat = newPath;
      await loadProductsTwice();
      toast(`✅ 已改为：${newPath}（${updates.length} 个产品跟着改）`, "ok");
    }
    closeModal("dlgCatEdit");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    busy(false);
  }
}

function openCatDel(ctx) {    // {mode:"del", path} | {mode:"purge", path}
  S.catDelCtx = ctx;
  const { rows } = treeRows();
  if (ctx.mode === "purge") {
    $("catDelTip").innerHTML =
      `此操作将会<b>彻底删除</b>该分类「${esc(ctx.path)}」，请谨慎操作`;
  } else {
    const node = rows.find((r) => r.path === ctx.path);
    const nSub = rows.filter((r) => r.path.startsWith(ctx.path + "/")).length;
    const nProd = S.items.filter((it) => {
      const c = String(it.cat || "");
      return c === ctx.path || c.startsWith(ctx.path + "/");
    }).length;
    $("catDelTip").innerHTML =
      `此操作将会删除该分类「${esc(ctx.path)}」，请谨慎操作` +
      (nSub ? `<br>其下 ${nSub} 个子分类会一起删除` : "") +
      (nProd ? `<br>分类下的 <b>${nProd}</b> 个产品会变成「未分类」（产品不会被删除）` : "");
  }
  $("dlgCatDel").classList.remove("hidden");
}

async function catDelOk() {
  const ctx = S.catDelCtx;
  if (!ctx) return;
  try {
    if (ctx.mode === "purge") {
      busy(true, "彻底删除…");
      await saveLib((lib) => {
        lib.deleted = lib.deleted.filter((d) => d.path !== ctx.path);
      });
      toast(`🗑 已彻底删除：${ctx.path}`, "ok");
    } else {
      busy(true, "删除分类…");
      const path = ctx.path;
      const updates = S.items
        .filter((it) => {
          const c = String(it.cat || "");
          return c === path || c.startsWith(path + "/");
        })
        .map((it) => ({ pid: it.pid, cat: "" }));
      if (updates.length) await api("/api/products/update", { updates });
      await saveLib((lib) => {
        lib.nodes = lib.nodes.filter((n) => {
          const full = n.parent ? n.parent + "/" + n.name : n.name;
          return full !== path && !full.startsWith(path + "/");
        });
        lib.deleted.push({ path, at: Date.now() });
      });
      if (S.cat === path || S.cat.startsWith(path + "/")) S.cat = "";
      if (S.viewCat === path || S.viewCat.startsWith(path + "/")) S.viewCat = "";
      await loadProductsTwice();
      toast(`🗑 已删除分类：${path}（进了回收站，可恢复）`, "ok");
    }
    closeModal("dlgCatDel");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    busy(false);
  }
}

async function recycleRestore(path) {
  try {
    busy(true, "恢复分类…");
    await saveLib((lib) => {
      /* 路径上缺的段都补回来（父分类被删过也不悬空） */
      let p = "";
      String(path).split("/").forEach((seg) => {
        const parent = p;
        p = p ? p + "/" + seg : seg;
        if (!lib.nodes.some((n) => n.parent === parent && n.name === seg)) {
          lib.nodes.push({ name: seg, parent });
        }
      });
      lib.deleted = lib.deleted.filter((d) => d.path !== path);
    });
    toast(`✅ 已恢复分类：${path}`, "ok");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    busy(false);
  }
}

function renderRecycle() {
  const box = $("recycleBox");
  if (!box || S.view !== "recycle") return;
  const list = [...(S.lib?.deleted || [])].sort((a, b) => (b.at || 0) - (a.at || 0));
  $("recycleEmpty").classList.toggle("hidden", list.length > 0);
  $("recycleBody").innerHTML = list.map((d) => {
    const p = String(d.path || "");
    const i = p.lastIndexOf("/");
    return `<tr>
      <td>${esc(i < 0 ? p : p.slice(i + 1))}</td>
      <td>${esc(i < 0 ? "—" : p.slice(0, i))}</td>
      <td>${fmtMs(Number(d.at) || 0)}</td>
      <td><button class="btn small" data-rstore="${esc(p)}">恢复</button>
          <button class="btn small danger" data-rdel="${esc(p)}">删除</button></td>
    </tr>`;
  }).join("");
}

/* ---------- 刊登词典（D1.5.0 智赢式）：侵权词 + 分类→商品类型 ---------- */

async function loadDict() {
  try {
    const [w, p, b] = await Promise.all([
      api("/api/dict", { kind: "words" }),
      api("/api/dict", { kind: "ptmap" }),
      api("/api/dict", { kind: "btmap" }),
    ]);
    S.words = (w.doc && w.doc.pairs) || [];
    S.ptmap = (p.doc && p.doc.map) || [];
    S.btmap = (b.doc && b.doc.map) || [];
  } catch (e) {
    /* 词典读不到不挡别的功能 */
    S.words = [];
    S.ptmap = [];
    S.btmap = [];
  }
  renderWords();
}

function renderWords() {
  if (S.view !== "words" || !$("wordsBox")) return;
  const ro = readOnly();
  document.querySelectorAll(".words-addrow").forEach((el) =>
    el.classList.toggle("hidden", ro));

  const wl = [...S.words].sort((a, b) => (b.at || 0) - (a.at || 0));
  $("wzEmpty").classList.toggle("hidden", wl.length > 0);
  $("wzBody").innerHTML = wl.map((w, i) => `<tr>
      <td>${i + 1}</td>
      <td><b style="color:#c62828">${esc(w.bad)}</b></td>
      <td>${esc(w.fix) || "<i style='color:#999'>（删除）</i>"}</td>
      <td>${fmtMs(Number(w.at) || 0)}${w.by ? " · " + esc(w.by) : ""}</td>
      <td>${ro ? "" : `<button class="btn small danger" data-wdel="${esc(w.bad)}">删除</button>`}</td>
    </tr>`).join("");

  const pl = [...S.ptmap].sort((a, b) => (b.at || 0) - (a.at || 0));
  $("ptEmpty").classList.toggle("hidden", pl.length > 0);
  $("ptBody").innerHTML = pl.map((m, i) => `<tr>
      <td>${i + 1}</td>
      <td>${esc(m.cat)}</td>
      <td><b>${esc(m.pt)}</b></td>
      <td>${fmtMs(Number(m.at) || 0)}${m.by ? " · " + esc(m.by) : ""}</td>
      <td>${ro ? "" : `<button class="btn small danger" data-pdel="${esc(m.cat)}">删除</button>`}</td>
    </tr>`).join("");

  const bl = [...S.btmap].sort((a, b) => (b.at || 0) - (a.at || 0));
  $("btEmpty").classList.toggle("hidden", bl.length > 0);
  $("btBody").innerHTML = bl.map((m, i) => `<tr>
      <td>${i + 1}</td>
      <td>${esc(m.cat)}</td>
      <td><b>${esc(m.node)}</b></td>
      <td>${esc(m.name)}</td>
      <td>${fmtMs(Number(m.at) || 0)}${m.by ? " · " + esc(m.by) : ""}</td>
      <td>${ro ? "" : `<button class="btn small danger" data-btdel="${esc(m.cat)}">删除</button>`}</td>
    </tr>`).join("");
}

async function saveDict(kind, doc) {
  await api("/api/dict", { kind, doc });
  await loadDict();
}

async function wzAddWord() {
  const bad = $("wzBad").value.trim();
  const fix = $("wzFix").value.trim();
  if (!bad) { toast("侵权词不能为空", "err"); return; }
  const old = S.words.find((w) => w.bad.toLowerCase() === bad.toLowerCase());
  if (old && !confirm(`「${bad}」已在表里（当前替换成「${old.fix || "删除"}」），要覆盖吗？`)) return;
  try {
    busy(true, "保存…");
    const pairs = S.words.filter((w) => w.bad.toLowerCase() !== bad.toLowerCase());
    pairs.push({ bad, fix, at: Date.now(), by: (S.profile || {}).user || "" });
    await saveDict("words", { pairs });
    $("wzBad").value = "";
    $("wzFix").value = "";
    toast(old ? "✅ 已更新" : "✅ 已添加（上传时自动替换）", "ok");
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

async function wzDelWord(bad) {
  if (!confirm(`确定删除侵权词「${bad}」？`)) return;
  try {
    busy(true, "删除…");
    await saveDict("words", {
      pairs: S.words.filter((w) => w.bad !== bad),
    });
    toast("🗑 已删除", "ok");
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

async function ptAddMap() {
  const cat = $("ptCat").value.trim();
  const pt = $("ptPt").value.trim();
  if (!cat || !pt) { toast("分类和商品类型都要填", "err"); return; }
  const old = S.ptmap.find((m) => m.cat === cat);
  if (old && old.pt !== pt && !confirm(`「${cat}」已映射到 ${old.pt}，改成 ${pt} 吗？`)) return;
  try {
    busy(true, "保存…");
    const map = S.ptmap.filter((m) => m.cat !== cat);
    map.push({ cat, pt, at: Date.now(), by: (S.profile || {}).user || "" });
    await saveDict("ptmap", { map });
    $("ptCat").value = "";
    $("ptPt").value = "";
    toast(old ? "✅ 已更新映射" : "✅ 已添加映射（上传时自动填类型）", "ok");
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

async function ptDelMap(cat) {
  if (!confirm(`确定删除「${cat}」的类型映射？`)) return;
  try {
    busy(true, "删除…");
    await saveDict("ptmap", { map: S.ptmap.filter((m) => m.cat !== cat) });
    toast("🗑 已删除", "ok");
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

/* D1.7.1 分类→亚马逊分类节点：上传时按产品分类自动带
   browse_classification（放进亚马逊对应货架） */
async function btAddMap() {
  const cat = $("btCat").value.trim();
  const node = $("btNode").value.trim();
  const name = $("btName").value.trim();
  if (!cat || !node) { toast("分类和节点 ID 都要填", "err"); return; }
  if (!/^\d+$/.test(node)) { toast("节点 ID 是纯数字（如 15684181）", "err"); return; }
  const old = S.btmap.find((m) => m.cat === cat);
  if (old && old.node !== node && !confirm(`「${cat}」已映射到节点 ${old.node}，改成 ${node} 吗？`)) return;
  try {
    busy(true, "保存…");
    const map = S.btmap.filter((m) => m.cat !== cat);
    map.push({ cat, node, name, at: Date.now(), by: (S.profile || {}).user || "" });
    await saveDict("btmap", { map });
    $("btCat").value = "";
    $("btNode").value = "";
    $("btName").value = "";
    toast(old ? "✅ 已更新映射" : "✅ 已添加映射（上传时自动放进这个亚马逊分类）", "ok");
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

async function btDelMap(cat) {
  if (!confirm(`确定删除「${cat}」的亚马逊分类映射？`)) return;
  try {
    busy(true, "删除…");
    await saveDict("btmap", { map: S.btmap.filter((m) => m.cat !== cat) });
    toast("🗑 已删除", "ok");
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

/* ---------- 侵权扫描（D1.5.0 智赢式：详情里查一遍 → 一键替换） ---------- */

function wordHits(text) {
  const low = String(text ?? "").toLowerCase();
  const out = [];
  for (const p of S.words || []) {
    const bad = String(p.bad || "").toLowerCase();
    if (!bad) continue;
    const n = low.split(bad).length - 1;
    if (n > 0) out.push({ bad: p.bad, fix: p.fix || "", n });
  }
  return out;
}

function openScan() {
  if (!D.rec) { toast("资料还在加载，稍等一下再点", "", 3000); return; }
  if (!(S.words || []).length) {
    toast("侵权词库还是空的：先到左边「🛡 侵权词库」里把踩过坑的品牌词加进去", "", 7000);
    return;
  }
  const rec = D.rec;
  const raw = rec.raw || [];
  const opt = rec.opt || {};
  const hits = [];

  const addAll = (where, text) => {
    wordHits(text).forEach((h) => hits.push({ where, ...h }));
  };

  raw.forEach((r, i) => {
    const rd = r.raw_data || {};
    const pre = raw.length > 1 ? `原始资料·第${i + 1}行·` : "原始资料·";
    addAll(pre + "标题", rd["标题(必填)"]);
    for (let b = 1; b <= 5; b++) addAll(pre + `要点${b}`, rd[`要点${b}`]);
    addAll(pre + "简介", rd["简介"]);
  });
  if (opt.title || (opt.bullets || []).length || opt.description) {
    addAll("AI结果·标题", opt.title);
    (opt.bullets || []).forEach((b, i) => addAll(`AI结果·要点${i + 1}`, b));
    addAll("AI结果·简介", opt.description);
  }

  SCAN.hits = hits;
  const name = esc(rec.title || rec.sku || rec.pid || "");
  $("scanTip").innerHTML = hits.length
    ? `「${name}」发现 <b>${hits.length}</b> 处侵权词。替换会写进已存资料（原始资料 + AI 结果都改）；上传亚马逊时也会按词表自动替换。`
    : `「${name}」里没发现侵权词，很干净 ✅`;
  $("scanBody").innerHTML = hits.map((h) => `<tr>
      <td>${esc(h.where)}</td>
      <td><b style="color:#c62828">${esc(h.bad)}</b></td>
      <td>${esc(h.fix) || "<i style='color:#999'>（删除）</i>"}</td>
      <td>${h.n}</td>
    </tr>`).join("");
  $("scanOk").classList.toggle("hidden", !hits.length || readOnly());
  $("dlgScan").classList.remove("hidden");
}

function applyWordsTo(t) {
  let s = String(t ?? "");
  for (const p of S.words || []) {
    if (!p.bad) continue;
    const rx = new RegExp(String(p.bad).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
    s = s.replace(rx, () => p.fix || "");
  }
  return s;
}

async function scanReplace() {
  const rec = D.rec;
  if (!rec || !(SCAN.hits || []).length) return;

  try {
    busy(true, "替换中…");
    let total = 0;

    // ① 原始资料：整份 raw 按行回写（要点位置不动，和手改文字同一原子写法）
    const raw = rec.raw || [];
    if (raw.length) {
      const rows = raw.map((r, i) => {
        const rd = r.raw_data || {};
        return {
          i,
          title: applyWordsTo(rd["标题(必填)"]),
          bullets: [1, 2, 3, 4, 5].map((b) => applyWordsTo(rd[`要点${b}`])),
          description: applyWordsTo(rd["简介"]),
        };
      });
      await api("/api/product/text", { pid: D.pid, rows });
      raw.forEach((r) => {
        const rd = r.raw_data || {};
        total += wordHits(rd["标题(必填)"]).length
          + [1, 2, 3, 4, 5].reduce(
            (s, b) => s + wordHits(rd[`要点${b}`]).length, 0)
          + wordHits(rd["简介"]).length;
      });
    }

    // ② AI 结果：短标题一起换；亮点/SEO 服务器端原样透传
    const opt = rec.opt || {};
    if (opt.title || (opt.bullets || []).length || opt.description) {
      const o2 = {
        title: applyWordsTo(opt.title),
        short_title: applyWordsTo(opt.short_title),
        bullets: (opt.bullets || []).map((b) => applyWordsTo(b)),
        description: applyWordsTo(opt.description),
      };
      if (o2.title || o2.bullets.filter(Boolean).length || o2.description) {
        await api("/api/product/opt", { pid: D.pid, opt: o2 });
        total += wordHits(opt.title).length
          + (opt.bullets || []).reduce((s, b) => s + wordHits(b).length, 0)
          + wordHits(opt.description).length;
      }
    }

    closeModal("dlgScan");
    toast(`🛡 已替换 ${total} 处`, "ok", 7000);
    await refillDetail();
    loadProductsTwice();
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

/* ---------- 移动分类（勾选产品 → 🏷 → 树里选，对齐智赢弹窗） ---------- */

function openMove(ctx = {}) {   // {} = 批量（S.sel）；{single:true, fill:"dCat"} = 单产品
  if (!ctx.single && S.sel.size === 0) { toast("先勾选产品", "err"); return; }
  S.moveCtx = { ...ctx, sel: null };
  $("moveTip").textContent = ctx.single
    ? "请选择分类"
    : `请为选中的 ${S.sel.size} 个产品选择分类`;
  S.treeQ = "";
  $("treeQ") && ($("treeQ").value = "");
  renderTree("#moveTree");
  $("dlgMove").classList.remove("hidden");
}

async function moveOk() {
  const ctx = S.moveCtx;
  if (!ctx) return;
  const sel = ctx.sel;
  if (sel === null || sel === undefined) { toast("先在树里点一个分类", "err"); return; }
  const cat = sel === "__none__" ? "" : sel;

  try {
    if (ctx.single) {
      const inp = $(ctx.fill);
      if (inp) inp.value = cat;
      closeModal("dlgMove");
      return;
    }
    busy(true, `移动 ${S.sel.size} 个产品…`);
    await api("/api/products/update", {
      updates: [...S.sel].map((pid) => ({ pid, cat })),
    });
    closeModal("dlgMove");
    await loadProductsTwice();
    toast(`✅ 已移动分类：${cat || "未分类"}`, "ok");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    busy(false);
  }
}

/* ---------- 三视图（产品列表 / 产品分类 / 分类回收站） ---------- */

function applyView() {
  const v = S.view;
  $("sidenav").querySelectorAll(".nav-item").forEach((el) =>
    el.classList.toggle("on", el.dataset.view === v));
  $("catPanel").classList.toggle("hidden", v === "list" || v === "words");
  $("catHead").classList.toggle("hidden", v !== "cat");
  $("recycleBox").classList.toggle("hidden", v !== "recycle");
  $("wordsBox").classList.toggle("hidden", v !== "words");
  $("selCat").classList.toggle("hidden", v === "cat" || v === "words");   // 分类视图用树筛，不重复

  const showProducts = v !== "recycle" && v !== "words";   // 回收站/词典视图只看自己的表格
  $("statusTabs").classList.toggle("hidden", !showProducts);
  $("dateTabs").classList.toggle("hidden", !showProducts);
  $("filterRow").classList.toggle("hidden", !showProducts);
  $("selbar").classList.toggle("hidden", !showProducts);
  $("grid").classList.toggle("hidden", !showProducts);
  $("pager").classList.toggle("hidden", !showProducts);
  $("importPanel").classList.add("hidden");
  if (!showProducts) $("empty").classList.add("hidden");
}

function setView(v) {
  S.view = v;
  if (v === "list") S.viewCat = "";
  renderAll();
}

function renderCatHead() {
  if (S.view !== "cat") return;
  const list = S.filtered || [];
  $("catHead").innerHTML =
    `<b>🗂 分类产品：${esc(catLabel(S.viewCat))}</b>` +
    `<span class="cat-head-n">${list.length} 个产品</span>` +
    (readOnly() ? "" : `　<span class="hint">勾选产品后点「🏷 移动分类」把它们分到这里</span>`);
}

async function applyDelete() {
  try {
    busy(true, `删除 ${S.sel.size} 个产品…`);
    await api("/api/products/delete", { pids: [...S.sel] });
    closeModal("dlgDel");
    S.sel.clear(); saveSel();
    await loadProductsTwice();
    toast(`🗑 已删除`, "ok");
  } catch (e) {
    toast(e.message, "err");
    loadProducts();
  } finally {
    busy(false);
  }
}

async function doExport() {
  try {
    busy(true, "导出优化结果…");
    const r = await api("/api/export", { pids: [...S.sel] });
    const bin = atob(r.data_b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([bytes], {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }));
    const a = document.createElement("a");
    a.href = url; a.download = r.filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    toast(`⬇️ 已导出 ${S.sel.size} 个产品`, "ok");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    busy(false);
  }
}

/* ---------- 导入 ---------- */

async function importXlsx() {
  const f = $("fileXlsx").files[0];
  if (!f) { toast("先选择 Excel 文件", "err"); return; }

  const b64 = await new Promise((ok, no) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result).split(",")[1] || "");
    r.onerror = no;
    r.readAsDataURL(f);
  });

  await runImport({ mode: "xlsx", name: f.name, data_b64: b64 });
}

async function importPaste() {
  await runImport({ mode: "paste", content: $("taPaste").value });
}

async function runImport(payload) {
  try {
    busy(true, "导入产品…（产品多时要一两分钟）");
    const r = await api("/api/import", payload);
    $("taPaste").value = "";
    $("fileXlsx").value = "";
    await loadProductsTwice();

    // 导入的产品不自动勾选（桌面版保持简单：导入后自己筛「待优化」）
    toast(`✅ 新增 ${r.added} · 更新 ${r.updated}（识别 ${r.rows} 行 → ${r.products} 个产品）`
      + (r.fat ? `；⚠️ ${r.fat} 个资料过大只存了基本信息` : ""), "ok", 6000);
  } catch (e) {
    toast(e.message, "err", 6000);
  } finally {
    busy(false);
  }
}

/* ---------- AI 优化（本地引擎） ---------- */

async function startOptimize() {
  if (!S.sel.size) { toast("请先勾选产品", "err"); return; }

  try {
    await api("/api/optimize", { pids: [...S.sel] });
    S.taskOpen = true;
    $("taskCard").classList.remove("hidden");
    pollTask();
    toast("🚀 任务已开始（AI 在本机后台跑，可以继续操作）", "ok");
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (e) {
    toast(e.message, "err");
  }
}

async function pollTask() {
  stopPoll();

  const tick = async () => {
    try {
      const r = await api("/api/optimize/status");
      const t = r.task || {};

      if (["reading", "running", "writing"].includes(t.phase)) {
        S.taskOpen = true;
      }
      if (S.taskOpen) renderTask(t);

      if (["reading", "running", "writing"].includes(t.phase)) {
        S.pollTimer = setTimeout(tick, 2500);
      } else if (t.phase === "done" && !t._done_toasted) {
        t._done_toasted = true;
        toast(t.message || "任务完成", "ok", 6000);
        loadProductsTwice();
      }
    } catch (e) { /* 断网等情况：下轮再试 */ S.pollTimer = setTimeout(tick, 5000); }
  };

  tick();
}

function stopPoll() {
  if (S.pollTimer) { clearTimeout(S.pollTimer); S.pollTimer = null; }
}

function renderTask(t) {
  $("taskCard").classList.remove("hidden");

  const running = ["reading", "running"].includes(t.phase);
  const paused = t.state === "paused";
  $("btnPause").classList.toggle("hidden", !(running && !paused));
  $("btnResume").classList.toggle("hidden", !(running && paused));
  $("btnCancel").classList.toggle("hidden", running || paused);
  $("btnWriteback").classList.toggle("hidden", t.phase !== "error" || !t.task_id);

  const total = Number(t.total) || 0;
  const done = Number(t.completed) || 0;
  const pct = total ? Math.min(100, Math.round(done / total * 100)) : 0;
  $("taskBar").style.width = t.phase === "done" ? "100%" : pct + "%";

  $("taskMsg").textContent = t.message || "";
  $("taskStat").textContent = total
    ? `${t.phase === "reading" ? "读资料" : t.phase === "writing" ? "写回" : "AI 处理"}`
      + ` ${done} / ${total} · 失败 ${Number(t.failed) || 0}`
      + (t.state ? ` · 状态 ${t.state}` : "")
    : "";
}

async function taskControl(cmd) {
  try {
    await api("/api/optimize/control", { cmd });
    toast(cmd === "pause" ? "⏸ 已暂停" : cmd === "resume" ? "▶ 已继续" : "⛔ 正在取消…");
    setTimeout(pollTask, 600);
  } catch (e) {
    toast(e.message, "err");
  }
}

async function retryWriteback() {
  try {
    busy(true, "写回产品…");
    const r = await api("/api/optimize/writeback", {});
    toast(`✅ 已写回 ${r.n} 个产品`, "ok");
    loadProductsTwice();
  } catch (e) {
    toast(e.message, "err");
  } finally {
    busy(false);
  }
}

/* ---------- D1.8.0 AI 归类：像 AI 优化一样一键跑，AI 只从
   自己的分类树里挑分类（不新建、挑不中不动） ---------- */

let acTimer = null;

async function startAutoCat() {
  if (!S.sel.size) { toast("请先勾选产品", "err"); return; }
  if (!window.confirm(
    `让 AI 给 ${S.sel.size} 个产品自动挑分类？\n`
    + `只从你已有的分类里挑（不会新建分类），挑不中的保持不变。`
  )) return;

  try {
    await api("/api/products/autocat", { pids: [...S.sel] });
    toast("🏷 AI 归类已开始（后台跑，可以继续操作）", "ok");
    pollAutoCat();
  } catch (e) {
    toast(e.message, "err");
  }
}

function pollAutoCat() {
  if (acTimer) { clearInterval(acTimer); acTimer = null; }
  const btn = $("btnAutoCat");

  acTimer = setInterval(async () => {
    let t;
    try {
      const r = await api("/api/products/autocat/status");
      t = r.task || {};
    } catch (e) { return; /* 断网：下轮再试 */ }

    if (t.phase === "running") {
      btn.disabled = true;
      btn.textContent =
        `🤖 AI 归类中 ${Number(t.done) || 0}/${Number(t.total) || 0}`;
      return;
    }
    clearInterval(acTimer); acTimer = null;
    btn.textContent = "🏷 AI 归类";
    renderSelBar();
    if (t.phase === "done") {
      toast("✅ " + (t.message || "AI 归类完成"), "ok", 8000);
      loadProductsTwice();
      renderTree();
    } else if (t.phase === "error") {
      toast(t.message || "AI 归类失败", "err", 8000);
    }
  }, 1800);
}

/* ---------- AI 设置 ---------- */

async function openAi() {
  try {
    const r = await api("/api/ai");
    $("aiStatus").textContent = r.key_len
      ? `🔒 当前使用${r.provider === "deepseek" ? "DeepSeek" : "OpenAI"} Key`
        + `（${r.key_len} 位，${r.source}）· 模型 ${r.model}`
      : "⚠️ 还没配置 Key：填一把保存，或让管理员在服务器保存全局 Key。";
    $("aiProvider").value = r.provider || "openai";
    $("aiKey").value = "";
    $("dlgAi").classList.remove("hidden");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function saveAi() {
  try {
    await api("/api/ai", {
      provider: $("aiProvider").value,
      key: $("aiKey").value.trim(),
    });
    closeModal("dlgAi");
    toast("✅ 已保存到本机", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ---------- V5（D1.4.1）：上传亚马逊（SP-API · 多店铺，密钥在服务器） ---------- */

async function openAmz() {
  $("dlgAmz").classList.remove("hidden");
  $("amzProg").classList.add("hidden");
  $("amzResult").innerHTML = "";
  $("amzCfgBox").classList.add("hidden");
  await refreshAmz();
}

async function refreshAmz() {
  try {
    const s = await api("/api/amazon/status");
    AZ.status = s;
    const stores = s.stores || [];
    const mkName = (id) => {
      const m = (s.markets || []).find(([i]) => i === id);
      return m ? m[1] : id;
    };

    $("amzStatus").innerHTML = stores.length
      ? `🏪 已绑定 <b>${stores.length}</b> 家店铺 · 任何电脑登录都能直接传`
      : (s.srv_err
        ? `❌ 读店铺清单失败：${esc(s.srv_err)}`
        : `⚠️ 还没绑定店铺（只需做一次）——点右边「➕ 绑定新店铺」`);

    $("azStore").innerHTML = stores.length
      ? stores.map((st) =>
          `<option value="${esc(st.id)}" ${st.id === s.active_store ? "selected" : ""}>`
          + `${esc(st.name)} · ${esc(mkName(st.marketplace))}</option>`).join("")
      : `<option value="">（还没绑定店铺）</option>`;
    $("azStoreDel").classList.toggle("hidden", !s.is_admin || !stores.length);
    $("azMgmtBtn").classList.toggle("hidden", !stores.length);
    $("azAppBox").classList.toggle("hidden", !s.is_admin || !!s.app_set);
    if (!$("azMgmt").classList.contains("hidden")) azMgmtRender();
    $("azEanBox").innerHTML = s.ean
      ? `🏷 EAN 条码池 · 前缀(厂商编号) <b>${esc(s.ean.prefix || "未初始化")}</b> 已锁定 · 已用 ${s.ean.used} · 剩余 ${s.ean.remaining} · 上传时自动补码，产品↔码永久绑定（重传不换码）`
      : "";

    // D1.7.0 按分类批量上传：已传清单（按店铺记在服务器上）
    AZ.up = s.uploaded || {};
    azByCatRender();

    $("azMarket").innerHTML = (s.markets || []).map(([id, label]) =>
      `<option value="${esc(id)}" ${id === s.marketplace ? "selected" : ""}>${esc(label)}</option>`).join("");
    $("azType").value = s.product_type === "PRODUCT" ? "" : s.product_type;

    // D1.5.0：分类→商品类型映射，自动填（精确路径优先，其次顶级段）
    if (!$("azType").value) {
      let pt = "";
      for (const pid of S.sel) {
        const it = (S.items || []).find((x) => x.pid === pid);
        const cat = it && it.cat;
        if (!cat) continue;
        const m = (S.ptmap || []).find((x) => x.cat === cat)
          || (S.ptmap || []).find((x) => cat.startsWith(x.cat + "/"));
        if (m) { pt = m.pt; break; }
      }
      if (pt) $("azType").value = pt;
    }

    $("azStart").textContent = `📤 开始上传（${S.sel.size} 个产品）`;
    $("azStart").disabled = !stores.length || S.sel.size === 0;

    const h = s.history || [];
    $("amzHistory").classList.toggle("hidden", !h.length);
    $("amzHistory").innerHTML = `<div class="d-sub">最近上传</div>`
      + h.map((e) => `<div class="d-imghint">${fmtMs(Number(e.at) || 0)} · ${esc(e.message || "")}</div>`).join("");

    const t = s.task;
    if (t && ["building", "uploading", "polling"].includes(t.phase)) {
      $("amzProg").classList.remove("hidden");
      azRenderTask(t);
      azPollStart();
    } else if (t && t.phase !== "idle" && t.result) {
      $("amzProg").classList.remove("hidden");
      azRenderTask(t);
    }
  } catch (e) {
    $("amzStatus").textContent = "读取店铺状态失败：" + e.message;
  }
}

async function azSelectStore() {
  try {
    await api("/api/amazon/store/select", { id: $("azStore").value });
    await refreshAmz();
  } catch (e) { toast(e.message, "err"); }
}

async function azUnbind() {
  const id = $("azStore").value;
  const st = ((AZ.status && AZ.status.stores) || []).find((x) => x.id === id);
  if (!id) return;
  if (!confirm(`确定解绑「${st ? st.name : id}」？解绑后所有电脑都不能再传这家店（可重新绑定）。`)) return;
  try {
    busy(true, "解绑…");
    await api("/api/amazon/store/delete", { id });
    toast("🗑 已解绑", "ok");
    await refreshAmz();
    setTimeout(refreshAmz, 8000);
    setTimeout(refreshAmz, 30000);
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

/* D1.6.0 智赢式授权：链接给用户复制 → 店铺自己的环境里登录同意 →
   把跳转网址粘回来。绝不在本机自动打开授权页（IP 不同=关联风险）。 */
async function azAuthorize() {
  try {
    const r = await api("/api/amazon/authorize/start", {
      name: $("azName").value.trim(),
      seller_id: $("azSeller").value.trim(),
      marketplace: $("azMarket").value,
    });
    $("azAuthUrl").value = r.url || "";
    $("azPaste").value = "";
    $("azReceipt").innerHTML = "";
    $("azPasteBox").classList.remove("hidden");
    toast("链接已生成：复制 → 去店铺自己的浏览器打开 → 同意 → 把跳转网址粘回来", "", 9000);
  } catch (e) {
    toast(e.message, "err", 7000);
  }
}

async function azCopy() {
  const t = $("azAuthUrl").value;
  if (!t) return;
  try {
    await navigator.clipboard.writeText(t);
    toast("📋 已复制，去这家店自己的浏览器里粘贴打开", "ok");
  } catch (e) {
    $("azAuthUrl").select();
    document.execCommand("copy");
    toast("📋 已复制，去这家店自己的浏览器里粘贴打开", "ok");
  }
}

async function azFinish() {
  const pasted = $("azPaste").value.trim();
  if (!pasted) { toast("先粘上「点同意之后」跳转到的完整网址", "err", 6000); return; }
  let r = null;
  try {
    busy(true, "绑定中…");
    try {
      r = await api("/api/amazon/authorize/finish", { url: pasted });
    } catch (e) {
      r = { ok: false, message: e.message };
    }
    azReceipt(r);
    if (r.ok) {
      toast("✅ 绑定成功！这家店在任何电脑都能传了", "ok", 6000);
      $("azPasteBox").classList.add("hidden");
      await refreshAmz();
      setTimeout(refreshAmz, 8000);    // KV 边缘同步最长约 60 秒，
      setTimeout(refreshAmz, 30000);   // 多看两遍兜底
    }
  } finally {
    busy(false);
  }
}

function azReceipt(r) {
  const st = r.store || {};
  $("azReceipt").innerHTML = r.ok
    ? `<div class="az-ok">✅ 授权成功！<b>${esc(st.name || "店铺")}</b> 已绑定并安全保存在服务器（换电脑免重绑）· ${fmtMs(Number(st.at) || 0)}</div>`
    : `<div class="warn">❌ ${esc(r.message || "绑定没成功，请重新走一遍")}</div>`;
}

async function azAppSave() {
  try {
    busy(true, "保存应用凭证…");
    await api("/api/amazon/app/save", {
      app_id: $("azAppId").value.trim(),
      client_id: $("azClientId").value.trim(),
      client_secret: $("azClientSecret").value.trim(),
    });
    $("azAppId").value = $("azClientId").value = $("azClientSecret").value = "";
    $("azAppBox").classList.add("hidden");
    toast("🔧 应用凭证已存到服务器（全公司一次就好），现在可以绑店了", "ok", 7000);
    await refreshAmz();
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

function azMgmtToggle() {
  $("azMgmt").classList.toggle("hidden");
  if (!$("azMgmt").classList.contains("hidden")) azMgmtRender();
}

function azMgmtRender() {
  const s = AZ.status || {};
  const stores = s.stores || [];
  const mkName = (id) => {
    const m = (s.markets || []).find(([i]) => i === id);
    return m ? m[1] : id;
  };
  $("azMgmtBody").innerHTML = stores.map((st) => `<tr>
      <td>${esc(st.name)}</td>
      <td>${esc(mkName(st.marketplace))}</td>
      <td>${esc(st.seller_id)}</td>
      <td>${fmtMs(Number(st.at) || 0)}</td>
      <td id="aztok-${esc(st.id)}">－</td>
      <td>
        <button class="btn small" onclick="azTestStore('${esc(st.id)}')">测连接</button>
        ${s.is_admin ? `<button class="btn small danger" onclick="azUnbindId('${esc(st.id)}','${esc(st.name)}')">解绑</button>` : ""}
      </td>
    </tr>`).join("");
}

async function azTestStore(id) {
  const cell = $("aztok-" + id);
  if (cell) cell.textContent = "测…";
  try {
    const r = await api("/api/amazon/store/test", { id });
    if (cell) cell.innerHTML = r.token_ok ? "✅ 可用" : `❌ ${esc((r.error || "失败").slice(0, 60))}`;
  } catch (e) {
    if (cell) cell.innerHTML = `❌ ${esc(e.message.slice(0, 60))}`;
  }
}

async function azUnbindId(id, name) {
  if (!confirm(`确定解绑「${name}」？解绑后所有电脑都不能再传这家店（可重新绑定）。`)) return;
  try {
    busy(true, "解绑…");
    await api("/api/amazon/store/delete", { id });
    toast("🗑 已解绑", "ok");
    await refreshAmz();
    setTimeout(refreshAmz, 8000);
    setTimeout(refreshAmz, 30000);
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

async function azStart() {
  await azUploadPids([...S.sel]);
}

/* D1.7.0 智赢式按分类批量上传：挑「该分类里没传过的」前 N 个一次传，
   传完整批由后端记「已上传」（记在这家店上，换电脑也不重传）。
   D1.7.2：默认再剔掉「分类没配亚马逊节点」的（防放错分类封号）。 */
function btNodeOf(cat) {
  /* 和后端 _bt_node_of 同一规则：精确命中优先，其次最长父级前缀 */
  const c0 = String(cat || "").trim();
  if (!c0 || !(S.btmap || []).length) return "";
  const ex = S.btmap.find((m) => m.cat === c0);
  if (ex) return ex.node;
  let best = "", bn = "";
  for (const m of S.btmap) {
    if (c0.startsWith(m.cat + "/") && m.cat.length > best.length) {
      best = m.cat; bn = m.node;
    }
  }
  return bn;
}

function azStrictOn() {
  const el = $("azStrict");
  return !!(el && el.checked);
}

function azCatItems(cat) {
  const strict = azStrictOn();
  const inCat = (S.items || []).filter((x) => x && x.pid
    && (!cat || (x.cat || "") === cat || (x.cat || "").startsWith(cat + "/")));
  const up = AZ.up || {};
  const eligible = inCat
    .filter((x) => !up[x.pid] && (Number(x.n_rows) || 0) > 0
      && (!strict || !!btNodeOf(x.cat)))
    .sort((a, b) => (a.created || 0) - (b.created || 0));
  return { inCat, eligible };
}

function azByCatRender() {
  const el = $("azCat");
  if (!el) return;
  const cur = el.value;
  // 分类选项 = 产品里实际用到的路径（含各级父分类）
  const cats = new Set();
  for (const x of (S.items || [])) {
    let acc = "";
    for (const seg of String((x && x.cat) || "").split("/")) {
      if (!seg) continue;
      acc = acc ? acc + "/" + seg : seg;
      cats.add(acc);
    }
  }
  const list = [...cats].sort();
  el.innerHTML = `<option value="">全部产品（含未分类）</option>`
    + list.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  if (cur && list.includes(cur)) el.value = cur;
  azByCatInfo();
}

function azByCatInfo() {
  const cat = $("azCat").value;
  const { inCat, eligible } = azCatItems(cat);
  const up = AZ.up || {};
  const done = inCat.filter((x) => up[x.pid]).length;
  let nodeTag = "";
  if (cat && azStrictOn()) {
    nodeTag = btNodeOf(cat)
      ? " · 节点✅"
      : " · ⚠️ 这个分类没配亚马逊节点（先去「🛡 侵权词库」配好才能传）";
  }
  $("azCatInfo").textContent =
    `共 ${inCat.length} 个 · 已传 ${done} · 可传 ${eligible.length}${nodeTag}`;
  let n = parseInt($("azBatchN").value, 10) || 50;
  n = Math.max(1, Math.min(200, n));
  $("azByCat").textContent = eligible.length
    ? `📤 按分类上传（传前 ${Math.min(n, eligible.length)} 个没传过的）`
    : "📤 这个分类没有可传的新产品了";
  $("azByCat").disabled = !(((AZ.status && AZ.status.stores) || []).length)
    || !eligible.length;
}

async function azByCatStart() {
  const cat = $("azCat").value;
  let n = parseInt($("azBatchN").value, 10) || 50;
  n = Math.max(1, Math.min(200, n));
  const pids = azCatItems(cat).eligible.slice(0, n).map((x) => x.pid);
  if (!pids.length) { toast("这个分类没有可传的新产品了", ""); return; }
  toast(`挑了 ${pids.length} 个没传过的产品，开始上传…`, "", 4000);
  await azUploadPids(pids);
}

async function azUploadPids(pids) {
  // D1.7.2 防错分类封号：默认拦下「分类没配亚马逊节点」的产品，
  // 让亚马逊自动猜分类有放错被封号的风险。勾去掉=自己担责放开。
  if (azStrictOn()) {
    const catOf = (pid) =>
      String(((S.items || []).find((x) => x.pid === pid) || {}).cat || "").trim();
    const okPids = pids.filter((p) => !!btNodeOf(catOf(p)));
    const skipped = pids.length - okPids.length;
    if (skipped) {
      const cats = [...new Set(pids.filter((p) => !btNodeOf(catOf(p)))
        .map(catOf).map((c) => c || "（未分类）"))];
      if (!okPids.length) {
        toast(`⛔ 这 ${skipped} 个产品的分类都没配「亚马逊分类节点」，不让亚马逊瞎猜分类（防封号）。去「🛡 侵权词库」第三张表配好：${cats.slice(0, 4).join("、")}`, "err", 9000);
        return;
      }
      toast(`⛔ 已拦下 ${skipped} 个产品没传（分类没配亚马逊节点，防放错分类封号）：${cats.slice(0, 4).join("、")}`, "", 8000);
      pids = okPids;
    }
  }
  try {
    busy(true, "提交上传…");
    await api("/api/amazon/upload", {
      pids,
      store: $("azStore").value,
      variant_mode: $("azVariant").value,
      product_type: $("azType").value.trim() || "PRODUCT",
      allow_unmapped: !azStrictOn(),
    });
    $("amzProg").classList.remove("hidden");
    $("amzResult").innerHTML = "";
    azPollStart();
  } catch (e) {
    toast(e.message, "err", 7000);
  } finally {
    busy(false);
  }
}

function azPollStart() {
  clearInterval(AZ.poll);
  AZ.poll = setInterval(async () => {
    try {
      const r = await api("/api/amazon/upload/status");
      azRenderTask(r.task);
      if (!["building", "uploading", "polling"].includes(r.task.phase)) {
        clearInterval(AZ.poll);
        loadProductsTwice();
        refreshAmz();   // D1.7.0：刷新已传清单，按分类的计数跟着更新
      }
    } catch (e) { /* 下轮再看 */ }
  }, 3000);
}

function azRenderTask(t) {
  if (!t || t.phase === "idle") return;
  $("amzProg").classList.remove("hidden");
  $("amzMsg").textContent = t.message || "";
  const res = t.result;
  if (!res) return;
  const rows = (res.skus || []).map((s) => `<tr>
      <td>${esc(s.sku)}</td>
      <td>${String(s.status).toLowerCase() === "success" ? "✅ 成功" : "❌ " + esc(s.status)}</td>
      <td>${esc((s.issues || []).map((i) => (i.code ? i.code + "：" : "") + i.message).join("；").slice(0, 400))}</td>
    </tr>`).join("");
  $("amzResult").innerHTML =
    `<div class="d-sub">结果：成功 ${res.success} · 失败 ${res.error}</div>`
    + ((res.feed_issues || []).length
      ? `<div class="warn">⚠️ ${esc(res.feed_issues.map((i) => i.message).join("；").slice(0, 400))}</div>` : "")
    + (rows ? `<table><tr><th style="width:150px">SKU</th><th style="width:110px">结果</th><th>原因</th></tr>${rows}</table>` : "")
    + ((res.notes || []).length
      ? `<div class="d-imghint">${res.notes.map(esc).join("<br>")}</div>` : "");
}

/* ---------- 弹窗 ---------- */

function closeModal(id) {
  $(id).classList.add("hidden");
  if (id === "dlgAmz") {
    clearInterval(AZ.poll);
    clearInterval(AZ.authPoll);
  }
}

// 详情弹窗关闭（D1.6.0 修复「✕ 点不掉」）：✕/遮罩一律直接关，
// 有没保存的图片调整也不再拦——改成放弃并提示（智赢也不拦）。
$("dlgDetail").addEventListener("click", (e) => {
  const closing = e.target === $("dlgDetail") || e.target.closest("[data-close]");
  if (closing && D.dirty) {
    D.dirty = false;
    toast("图片调整还没保存，已放弃", "", 4000);
  }
}, true);

// D1.6.1 修复「照片点开关不掉」：大图预览的类名是 .lightbox 不是
// .modal，之前根本没挂上关闭逻辑（✖ 和点空白都无效）。一起挂上，
// 另加 Esc 关闭。
document.querySelectorAll(".modal, .lightbox").forEach((m) => {
  m.addEventListener("click", (e) => {
    if (e.target === m || e.target.closest("[data-close]")) m.classList.add("hidden");
  });
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const lb = $("dlgLightbox");
  if (lb && !lb.classList.contains("hidden")) lb.classList.add("hidden");
});

/* ---------- 事件绑定 ---------- */

function bindEvents() {
  $("loginBtn").onclick = doLogin;
  $("loginPass").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
  $("btnLogout").onclick = doLogout;
  $("btnRefresh").onclick = () => { busy(true, "刷新…"); loadProducts(true).finally(() => busy(false)); };
  $("btnAi").onclick = openAi;
  $("btnAiSave").onclick = saveAi;

  $("statusTabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-st]");
    if (!b) return;
    S.status = b.dataset.st; S.page = 0; renderAll();
  });

  $("dateTabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-dt]");
    if (!b) return;
    S.date = b.dataset.dt; S.page = 0; renderAll();
  });

  $("selCat").onchange = (e) => { S.cat = e.target.value; S.page = 0; renderAll(); };
  $("selSort").onchange = (e) => { S.sort = e.target.value; renderAll(); };
  $("selPer").onchange = (e) => { S.per = +e.target.value; S.page = 0; renderAll(); };
  $("selScope").onchange = (e) => { S.scope = e.target.value; loadProducts(); };

  let qTimer = null;
  $("inpQ").oninput = (e) => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { S.q = e.target.value; S.page = 0; renderAll(); }, 200);
  };

  $("grid").addEventListener("click", (e) => {
    const card = e.target.closest(".card");
    if (!card) return;
    const btn = e.target.closest("[data-act]");

    if (btn && btn.dataset.act === "sel") {
      const pid = card.dataset.pid;
      if (S.sel.has(pid)) S.sel.delete(pid); else S.sel.add(pid);
      saveSel(); renderGrid(); renderSelBar();
      return;
    }

    openDetail(card.dataset.pid);   // 点图片/标题/卡片任何地方 → 立刻展开详情
  });

  $("btnSelAll").onclick = () => {
    const list = S.filtered || [];
    const slice = list.slice(S.page * S.per, (S.page + 1) * S.per);
    slice.forEach((it) => S.sel.add(String(it.pid)));
    saveSel(); renderGrid(); renderSelBar();
  };
  $("btnSelNone").onclick = () => { S.sel.clear(); saveSel(); renderGrid(); renderSelBar(); };

  $("btnOpt").onclick = startOptimize;
  $("btnAutoCat").onclick = startAutoCat;           // 🏷 AI 归类（D1.8.0）
  $("btnCat").onclick = () => openMove();           // 🏷 移动分类（树选择）
  $("moveOk").onclick = moveOk;
  $("ceOk").onclick = catEditOk;
  $("catDelOk").onclick = catDelOk;

  /* 侧栏视图（产品列表 / 产品分类 / 分类回收站 / 侵权词库） */
  $("sidenav").addEventListener("click", (e) => {
    const it = e.target.closest(".nav-item");
    if (it) setView(it.dataset.view);
  });

  /* 刊登词典（D1.5.0）：添加/删除 + 侵权扫描确认 */
  $("wzAdd").onclick = wzAddWord;
  $("ptAdd").onclick = ptAddMap;
  $("btAdd").onclick = btAddMap;
  $("wordsBox").addEventListener("click", (e) => {
    const d = e.target.closest("[data-wdel]");
    if (d) { wzDelWord(d.dataset.wdel); return; }
    const p = e.target.closest("[data-pdel]");
    if (p) { ptDelMap(p.dataset.pdel); return; }
    const bt = e.target.closest("[data-btdel]");
    if (bt) btDelMap(bt.dataset.btdel);
  });
  $("scanOk").onclick = scanReplace;

  /* 分类树：点行选中筛选、箭头展开、悬停图标 CRUD */
  $("treeQ").oninput = (e) => {
    S.treeQ = e.target.value;
    renderTree();
  };
  $("catTree").addEventListener("click", (e) => {
    const tib = e.target.closest("[data-tact]");
    const tw = e.target.closest("[data-toggle]");
    const row = e.target.closest(".tree-node");
    if (tib && row) {
      const path = row.dataset.zpath;
      const { rows } = treeRows();
      const node = rows.find((r) => r.path === path);
      const name = node ? node.name : path.slice(path.lastIndexOf("/") + 1);
      if (tib.dataset.tact === "add") openCatEdit({ mode: "add", parent: path });
      if (tib.dataset.tact === "ren") openCatEdit({ mode: "ren", path, name });
      if (tib.dataset.tact === "del") openCatDel({ mode: "del", path });
      return;
    }
    if (tw) {
      const p = tw.dataset.toggle;
      if (S.expanded.has(p)) S.expanded.delete(p); else S.expanded.add(p);
      localStorage.setItem("wz_tree_open", JSON.stringify([...S.expanded]));
      renderTree();
      return;
    }
    if (row && row.dataset.zpath !== undefined) {
      S.viewCat = row.dataset.zpath;
      S.page = 0;
      renderAll();
    }
  });

  /* 移动分类弹窗树：单选（未分类行 data-pick，节点行 data-zpath） */
  $("moveTree").addEventListener("click", (e) => {
    const tw = e.target.closest("[data-toggle]");
    if (tw) return;                       // 弹窗里树全展开，箭头不动
    const row = e.target.closest(".tree-node");
    if (!row) return;
    S.moveCtx.sel = row.dataset.pick || row.dataset.zpath;
    renderTree("#moveTree");
  });

  /* 回收站：恢复 / 彻底删除 */
  $("recycleBody").addEventListener("click", (e) => {
    const rs = e.target.closest("[data-rstore]");
    const rd = e.target.closest("[data-rdel]");
    if (rs) recycleRestore(rs.dataset.rstore);
    if (rd) openCatDel({ mode: "purge", path: rd.dataset.rdel });
  });
  $("btnDel").onclick = () => { $("delCount").textContent = S.sel.size; $("dlgDel").classList.remove("hidden"); };
  $("btnDelApply").onclick = applyDelete;
  $("btnExport").onclick = doExport;
  $("btnAmz").onclick = openAmz;
  $("azBind").onclick = () => $("amzCfgBox").classList.toggle("hidden");
  $("azStore").onchange = azSelectStore;
  $("azStoreDel").onclick = azUnbind;
  // D1.7.0 按分类批量上传 + D1.7.2 防错分类开关联动计数
  $("azByCat").onclick = azByCatStart;
  $("azStrict").onchange = azByCatInfo;
  $("azCat").onchange = () => {
    const cat = $("azCat").value;
    if (cat && !$("azType").value) {
      const m = (S.ptmap || []).find((x) => x.cat === cat)
        || (S.ptmap || []).find((x) => cat.startsWith(x.cat + "/"));
      if (m) $("azType").value = m.pt;
    }
    azByCatInfo();
  };
  $("azBatchN").oninput = azByCatInfo;
  $("azAuth").onclick = azAuthorize;
  $("azCopy").onclick = azCopy;
  $("azFinish").onclick = azFinish;
  $("azAppSave").onclick = azAppSave;
  $("azMgmtBtn").onclick = azMgmtToggle;
  $("azStart").onclick = azStart;

  $("btnImportToggle").onclick = () => $("importPanel").classList.toggle("hidden");
  $("btnImportXlsx").onclick = importXlsx;
  $("btnImportPaste").onclick = importPaste;

  $("btnPrev").onclick = () => { S.page--; renderGrid(); renderPager(); renderSelBar(); window.scrollTo({ top: 0 }); };
  $("btnNext").onclick = () => { S.page++; renderGrid(); renderPager(); renderSelBar(); window.scrollTo({ top: 0 }); };
  $("btnJump").onclick = () => {
    const pages = Math.max(1, Math.ceil((S.filtered || []).length / S.per));
    S.page = Math.max(0, Math.min(+$("inpJump").value || 1, pages) - 1);
    renderGrid(); renderPager(); renderSelBar(); window.scrollTo({ top: 0 });
  };

  $("btnPause").onclick = () => taskControl("pause");
  $("btnResume").onclick = () => taskControl("resume");
  $("btnCancel").onclick = () => taskControl("cancel");
  $("btnWriteback").onclick = retryWriteback;
  $("btnCloseTask").onclick = () => {
    S.taskOpen = false;
    $("taskCard").classList.add("hidden");
  };

  /* ----- V3：添加图片 / 图片编辑器 ----- */
  $("iaUrlOk").onclick = () => {
    const u = $("iaUrl").value.trim();
    if (!/^https?:\/\//i.test(u)) { toast("先粘贴 http/https 开头的图片网址", "err"); return; }
    D.imgs.push(u.slice(0, 500));
    markDirty();
    closeModal("dlgImgAdd");
    renderSlots();
    toast("已加入图片集（点「💾 保存图片修改」生效）");
  };

  $("iaFileOk").onclick = async () => {
    const f = $("iaFile").files[0];
    if (!f) { toast("先选择一个图片文件", "err"); return; }
    if (f.size > 8 * 1024 * 1024) { toast("图片太大（超过 8MB）", "err"); return; }
    const dataUrl = await new Promise((ok, no) => {
      const r = new FileReader();
      r.onload = () => ok(String(r.result));
      r.onerror = no;
      r.readAsDataURL(f);
    }).catch(() => "");
    closeModal("dlgImgAdd");
    if (dataUrl) openImgEditor(-1, true, dataUrl);
    else toast("本地文件读取失败", "err");
  };

  $("ieRotL").onclick = () => rotateWork(-1);
  $("ieRotR").onclick = () => rotateWork(1);
  $("ieCrop").onclick = startCrop;
  $("ieCropOk").onclick = applyCrop;
  $("ieCropCancel").onclick = endCrop;
  $("ieWhite").onclick = whiteMain;
  $("ieReset").onclick = () => {
    if (IE.work && IE.init) {
      IE.work = copyCanvas(IE.init);
      IE.zoom = 1;
      draw();
      toast("已还原到刚打开时的样子");
    }
  };
  $("ieDl").onclick = () => {
    if (!IE.work) return;
    const a = document.createElement("a");
    a.href = IE.work.toDataURL("image/jpeg", 0.92);
    a.download = `${D.pid || "image"}_${Date.now() % 10000}.jpg`;
    a.click();
  };
  $("ieUse").onclick = useEdited;

  const stage = $("ieStage");
  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    if (!IE.work) return;
    IE.zoom = Math.max(0.2, Math.min(6, IE.zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
    draw();
  }, { passive: false });

  let cropDrag = null;
  stage.addEventListener("mousedown", (e) => {
    if (IE.mode !== "crop" || !IE.work) return;
    const cr = $("ieCanvas").getBoundingClientRect();
    cropDrag = {
      cr,
      x0: Math.min(Math.max(e.clientX - cr.left, 0), cr.width),
      y0: Math.min(Math.max(e.clientY - cr.top, 0), cr.height),
    };
    e.preventDefault();
  });
  stage.addEventListener("mousemove", (e) => {
    if (!cropDrag) return;
    const { cr, x0, y0 } = cropDrag;
    const x1 = Math.min(Math.max(e.clientX - cr.left, 0), cr.width);
    const y1 = Math.min(Math.max(e.clientY - cr.top, 0), cr.height);
    const sr = stage.getBoundingClientRect();
    const box = $("ieCropBox");
    box.classList.remove("hidden");
    box.style.left = (cr.left - sr.left + Math.min(x0, x1)) + "px";
    box.style.top = (cr.top - sr.top + Math.min(y0, y1)) + "px";
    box.style.width = Math.abs(x1 - x0) + "px";
    box.style.height = Math.abs(y1 - y0) + "px";
    const kx = IE.work.width / cr.width;
    const ky = IE.work.height / cr.height;
    $("ieCropSize").textContent =
      `裁剪后约 ${Math.round(Math.abs(x1 - x0) * kx)}×${Math.round(Math.abs(y1 - y0) * ky)} 像素`;
  });
  window.addEventListener("mouseup", () => { cropDrag = null; });
}

bindEvents();
boot();
