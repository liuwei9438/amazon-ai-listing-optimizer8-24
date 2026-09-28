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
};

/* D = 当前详情弹窗的图片编辑状态；IE = 图片编辑器状态 */
const D = { pid: "", imgs: [], dirty: false, thin: false };
const IE = { work: null, init: null, idx: -1, isNew: false, zoom: 1, mode: "view" };

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
  loadProducts()                        // 磁盘缓存瞬间出列表
    .then(() => {
      const m = location.hash.match(/^#pid=(.+)$/);  // 深链/自测：#pid=xxx 直接开详情
      if (m) openDetail(decodeURIComponent(m[1]));
    });
  setTimeout(() => loadProducts(true), 1200);  // 随后拉最新
  pollTask();  // 万一上次任务还在跑
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

  let list = S.items.filter((it) => {
    if (S.status && stateOf(it) !== S.status) return false;

    if (S.cat && (it.cat || "未分类") !== S.cat) return false;

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
  const cats = [...S.cats];
  if (S.items.some((it) => !String(it.cat || "").trim())
      && !cats.includes("未分类")) cats.push("未分类");

  $("selCat").innerHTML =
    `<option value="">全部分类</option>` +
    cats.map((c) =>
      `<option value="${esc(c)}" ${S.cat === c ? "selected" : ""}>${esc(c)}</option>`
    ).join("");
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
      `分类：${esc(String(it.cat || "未分类").slice(0, 20))}（资料 ${Number(it.n_rows) || 0} 行）`,
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
  ["btnSelAll", "btnSelNone", "btnOpt", "btnCat", "btnDel",
   "btnExport", "btnImportToggle"].forEach((id) =>
    $(id).classList.toggle("hidden", ro));

  if (ro) {
    $("selCount").textContent = "👁 只读视图 — 切回「我的库」才能操作";
    return;
  }
  $("selCount").textContent = `已选 ${S.sel.size} 个（跨页保留）`;
  $("btnOpt").disabled = S.sel.size === 0;
  $("btnCat").disabled = S.sel.size === 0;
  $("btnDel").disabled = S.sel.size === 0;
  $("btnExport").disabled = S.sel.size === 0;
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
      <div class="grow">分类<input id="dCat" class="inp" value="${esc(cat || "")}" maxlength="40"></div>
      <button id="dSave" class="btn primary">💾 保存标记</button>
      <button id="dOpt1" class="btn">🚀 优化这个产品</button>
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
    parts.push(`<div class="d-sec"><h4>🤖 AI 优化结果</h4>${sec.join("")}</div>`);
  } else {
    parts.push(`<div class="d-sec"><h4>🤖 AI 优化结果</h4><div class="txt" style="color:#999">还没优化过。点上面「🚀 优化这个产品」，跑完结果自动挂上来。</div></div>`);
  }

  // 资料明细：采集/导入的 20 列原始资料全部展示
  if (raw.length) {
    parts.push(`<div class="d-sec"><h4>📋 资料明细（原始资料 ${raw.length} 行，点行标题展开/收起）</h4>`
      + raw.map((r, idx) => {
          const rd = r.raw_data || {};
          const label = `第 ${idx + 1} 行 · ${String(rd["SKU"] || rd["父SKU(必填)"] || "—").slice(0, 24)} · ${String(rd["标题(必填)"] || "").slice(0, 34)}`;
          const rows = Object.entries(rd)
            .filter(([k, v]) => v !== "" && v != null && k !== "产品图" && k !== "简介图")
            .map(([k, v]) => `<tr><td class="k">${esc(k)}</td><td>${
              k === "参考网址" && String(v).startsWith("http")
                ? `<a href="${esc(String(v))}" target="_blank" rel="noopener">${esc(String(v).slice(0, 60))}</a>`
                : esc(String(v))
            }</td></tr>`)
            .join("");
          return `<details class="rawrow"${idx === 0 ? " open" : ""}><summary>${esc(label)}</summary><table>${rows || '<tr><td>（空行）</td></tr>'}</table></details>`;
        }).join("")
      + `</div>`);
  } else {
    parts.push(`<div class="d-sec"><div class="warn">⚠️ 资料不全：这条产品没存完整资料。用「📥 导入产品」传同一份 Excel 即可补全（相同 SKU 自动合并，不会重复）。</div></div>`);
  }

  // 变体
  const vars = rec.variants || [];
  if (vars.length) {
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

/* ---------- 批量动作 ---------- */

async function applyCat() {
  const name = $("catInput").value.trim();
  if (!name) { toast("先填分类名", "err"); return; }

  try {
    busy(true, "设置分类…");
    await api("/api/products/update", {
      updates: [...S.sel].map((pid) => ({ pid, cat: name })),
    });
    closeModal("dlgCat");
    await loadProductsTwice();
    toast(`✅ 已设置分类：${name}`, "ok");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    busy(false);
  }
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

/* ---------- 弹窗 ---------- */

function closeModal(id) { $(id).classList.add("hidden"); }

// 详情弹窗防手滑：图片改了没保存就点关闭 → 先问一句。
// 必须注册在下面通用关闭逻辑之前（同一元素上先注册先执行），
// 用 stopImmediatePropagation 拦掉通用关闭。
$("dlgDetail").addEventListener("click", (e) => {
  const closing = e.target === $("dlgDetail") || e.target.closest("[data-close]");
  if (closing && D.dirty && !confirm("图片修改还没保存，关闭会丢掉这些调整。确定关闭？")) {
    e.preventDefault();
    e.stopImmediatePropagation();
  }
}, true);

document.querySelectorAll(".modal").forEach((m) => {
  m.addEventListener("click", (e) => {
    if (e.target === m || e.target.closest("[data-close]")) m.classList.add("hidden");
  });
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
  $("btnCat").onclick = () => { $("catInput").value = ""; $("dlgCat").classList.remove("hidden"); };
  $("btnCatApply").onclick = applyCat;
  $("btnDel").onclick = () => { $("delCount").textContent = S.sel.size; $("dlgDel").classList.remove("hidden"); };
  $("btnDelApply").onclick = applyDelete;
  $("btnExport").onclick = doExport;

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
