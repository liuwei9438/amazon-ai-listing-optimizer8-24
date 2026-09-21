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
  loadProducts();                       // 磁盘缓存瞬间出列表
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
        ${ro ? "" : `<button class="btn ${on ? "on" : ""}" data-act="sel">${on ? "☑ 已选中" : "☐ 选择"}</button>`}
        <button class="btn" data-act="det">📄 详情</button>
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

/* ---------- 详情 ---------- */

async function openDetail(pid) {
  try {
    busy(true, "读取产品…");
    const it = S.items.find((x) => String(x.pid) === String(pid)) || {};
    const owner = readOnly() ? String(it.owner || "") : "";
    const r = await api(
      `/api/product?pid=${encodeURIComponent(pid)}${owner ? `&owner=${encodeURIComponent(owner)}` : ""}`
    );
    renderDetail(r.product || {}, it);
    $("dlgDetail").classList.remove("hidden");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    busy(false);
  }
}

function renderDetail(rec, it) {
  const opt = rec.opt || {};
  const raw = rec.raw || [];
  const ro = readOnly();
  const status = ["wait", "found", "none"].includes(String(rec.status)) ? String(rec.status) : "wait";
  const st = stateOf(it.pid ? it : { has_opt: !!opt.title, n_rows: raw.length });

  const img = String(opt.image || rec.img || "").trim();
  const parts = [];

  parts.push(`<div class="d-head">
    <div class="d-img">${img
      ? `<img referrerpolicy="no-referrer" src="${esc(img)}" onerror="this.remove()">`
      : "（无图片）"}</div>
    <div class="d-info">
      <div class="line"><b>${esc(String(rec.title || "（无标题）").slice(0, 90))}</b></div>
      <div class="line">SKU：<b>${esc(rec.sku || "—")}</b>　型号：<b>${esc(rec.model || "—")}</b></div>
      <div class="line">分类：<b>${esc(rec.cat || "未分类")}</b>　资料：<b>${raw.length} 行</b>
      　状态：<b>${BADGE[st][0]}</b></div>
      ${opt.at ? `<div class="line">上次优化：${fmtMs(opt.at)}</div>` : ""}
      ${readOnly() && rec.owner ? `<div class="line">👤 ${esc(rec.owner)}</div>` : ""}
    </div>
  </div>`);

  if (!ro) {
    parts.push(`<div class="d-edit">
      <div>标记<select id="dStatus" class="inp">
        <option value="wait" ${status === "wait" ? "selected" : ""}>⚪ 待找货</option>
        <option value="found" ${status === "found" ? "selected" : ""}>✅ 已找到</option>
        <option value="none" ${status === "none" ? "selected" : ""}>❌ 没找到</option>
      </select></div>
      <div class="grow">分类<input id="dCat" class="inp" value="${esc(rec.cat || "")}" maxlength="40"></div>
      <button id="dSave" class="btn primary">💾 保存标记</button>
      <button id="dOpt1" class="btn">🚀 优化这个产品</button>
    </div>`);
  }

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
  } else if (raw.length) {
    parts.push(`<div class="d-sec"><h4>🤖 AI 优化结果</h4><div class="txt" style="color:#999">还没优化过。勾选后点「🚀 AI 优化」，结果会挂在这个产品上。</div></div>`);
  } else {
    parts.push(`<div class="d-sec"><div class="warn">⚠️ 资料不全：旧库数据没有存完整资料。重新导入同一份 Excel 即可补全（相同 SKU 自动合并）。</div></div>`);
  }

  const vars = rec.variants || [];
  if (vars.length) {
    parts.push(`<div class="d-sec"><h4>变体（${vars.length} 个）</h4>
      <table><tr><th>SKU</th><th>属性</th><th>标题</th></tr>
      ${vars.map((v) => `<tr><td>${esc(v.sku || "")}</td><td>${esc(v.attr || "")}</td><td>${esc(String(v.title || "").slice(0, 40))}</td></tr>`).join("")}
      </table></div>`);
  }

  $("dBody").innerHTML = parts.join("");
  $("dTitle").textContent = "📋 产品详情";
  $("dBody").dataset.pid = String(rec.pid || it.pid || "");

  const bind = (id, fn) => {
    const el = $(id);
    if (el) el.onclick = fn;
  };
  bind("dSave", saveDetailMark);
  bind("dOpt1", () => {
    const pid = $("dBody").dataset.pid;
    S.sel = new Set([pid]);
    saveSel();
    closeModal("dlgDetail");
    startOptimize();
  });
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
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const pid = btn.closest(".card").dataset.pid;

    if (btn.dataset.act === "det") {
      openDetail(pid);
    } else {
      if (S.sel.has(pid)) S.sel.delete(pid); else S.sel.add(pid);
      saveSel(); renderGrid(); renderSelBar();
    }
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
}

bindEvents();
boot();
