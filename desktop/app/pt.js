/* pt.js —— 🧭 亚马逊 AI 分类（模块 8 UI，独立文件：不改 app.js 逻辑）
   页面 A 商品列表 / B 分类详情（侧板）/ C 批量任务中心 / D 人工复核。
   数据与状态安全铁律（批复七）：
   - mock 结果一律显示「模拟数据」；未官方验证绝不显示「官方验证通过」；
   - 任何地方都不出现「可以刊登」字样（缺口报告只做对照展示）；
   - 人工确认显示「👤 人工确认」，不冒充官方验证。
   状态真源=服务器 SQLite：刷新/重开页面都从 /api/pt/* 重新拉。 */
"use strict";

const PT = {
  inited: false, overview: null, stores: [], store: "", env: "live",
  status: {},            // pid -> {state, pt, chosen_by, confidence, task_state, error_kind}
  filter: "all", q: "", sel: new Set(),
  task: null, taskId: "", poll: null, viewing: "list", items: [],
  detailPid: "", detail: null,
};

const PT_FILTERS = [
  ["all", "全部"], ["todo", "未分类"], ["done", "已分类"],
  ["review", "待复核"], ["failed", "失败"],
];

const PT_PHASE_LABEL = {
  created: "⚪ 已创建", running: "🔄 进行中", pausing: "⏸ 暂停中…",
  paused: "⏸ 已暂停", completed: "✅ 已完成",
  completed_with_issues: "⚠️ 完成但有待处理",
  failed: "❌ 失败", interrupted: "🔌 中断（可续跑）",
  cancelling: "⛔ 取消中…", cancelled: "⛔ 已取消",
};

const PT_ITEM_LABEL = {
  pending: "⚪ 排队", running: "🔄 处理中", completed: "✅ 完成",
  needs_review: "👁 待复核", retryable_failed: "🔁 可重试失败",
  permanent_failed: "❌ 失败", cancelled: "⛔ 取消",
};

const PT_ACTIVE = new Set(["created", "running", "pausing", "cancelling"]);

function pt$(id) { return document.getElementById(id); }

function ptStopPoll() {
  if (PT.poll) { clearInterval(PT.poll); PT.poll = null; }
}

/* ---------- 进入视图 / 初始化 ---------- */

window.PtUI = {
  enter() {
    if (!PT.inited) { ptBind(); PT.inited = true; }
    ptRefreshAll();
  },
};

async function ptRefreshAll() {
  await ptLoadOverview();
  await ptLoadStatus();
  await ptRestoreTask();      // 刷新/重开：任务中心从库里恢复
  ptRender();
}

async function ptLoadOverview() {
  try {
    const r = await api("/api/pt/overview");
    PT.overview = r;
    PT.stores = r.stores || [];
    if (!PT.store) {
      PT.store = r.active_store || (PT.stores[0] || {}).id || "";
    }
    if (!(r.env || {}).mock) {          // 服务器没 mock 能力就不给选
      const sel = pt$("ptEnv");
      for (const op of [...sel.options]) if (op.value === "mock") op.remove();
      if (PT.env === "mock") PT.env = "live";
    }
    const ai = r.ai || {};
    pt$("ptAiState").textContent = ai.ready
      ? `AI：${ai.provider} / ${ai.model}`
      : "AI 未配置（⚙️ 填 Key 或管理员配全局 Key；未配置时商品会进人工复核）";
  } catch (e) {
    pt$("ptAiState").textContent = "pt 服务不可用：" + e.message;
  }
  const sel = pt$("ptStore");
  sel.innerHTML = PT.stores.map((s) =>
    `<option value="${esc(s.id)}">${esc(s.name || s.id)}（${esc(s.marketplace)}）</option>`).join("");
  sel.value = PT.store;
}

async function ptLoadStatus() {
  if (!PT.store) { PT.status = {}; return; }
  try {
    const r = await api(`/api/pt/status?store=${encodeURIComponent(PT.store)}&env=${PT.env}`);
    PT.status = r.status || {};
  } catch (e) { /* 401 已由 api() 处理；其余静默（顶栏已示状态） */ }
  pt$("ptMockBadge").classList.toggle("hidden", PT.env !== "mock");
}

/* ---------- 任务中心（C） ---------- */

async function ptRestoreTask() {
  try {
    const r = await api("/api/pt/tasks?limit=15");
    const ts = r.tasks || [];
    ptRenderTaskList(ts);
    const act = ts.find((t) => PT_ACTIVE.has(t.phase)) || ts[0];
    if (act) { PT.task = act; PT.taskId = act.id; }
    if (act && PT_ACTIVE.has(act.phase)) ptStartPoll();
  } catch (e) { /* ignore */ }
  ptRenderTaskCard();
}

function ptStartPoll() {
  ptStopPoll();
  PT.poll = setInterval(ptTickTask, 1800);
}

/* task_detail 返回 {task: 行, progress: 进度}，合并成一张给卡片用 */
function ptMergeTask(r) {
  return Object.assign({}, r.task || {}, r.progress || {});
}

async function ptTickTask() {
  if (!PT.taskId) return ptStopPoll();
  try {
    const r = await api(`/api/pt/tasks/${encodeURIComponent(PT.taskId)}`);
    PT.task = ptMergeTask(r);
    if (!PT_ACTIVE.has(r.task.phase)) {
      ptStopPoll();
      await ptLoadStatus();
      if (PT.viewing === "task") await ptLoadTaskItems();
      toast("🧭 分类任务已结束：" + (PT_PHASE_LABEL[r.task.phase] || r.task.phase));
    }
    ptRenderTaskCard();
    if (PT.viewing === "task") ptRenderItems();
  } catch (e) { ptStopPoll(); }
}

function ptRenderTaskCard() {
  const card = pt$("ptTaskCard");
  const t = PT.task;
  if (!t) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const total = Number(t.total) || 0;
  const finished = Math.max(0, total - (Number(t.pending) || 0)
    - (Number(t.running) || 0));
  const p = t.progress_percent != null ? Math.round(t.progress_percent)
    : (total ? Math.round(finished * 100 / total) : 0);
  pt$("ptTaskPhase").textContent = PT_PHASE_LABEL[t.phase] || t.phase;
  pt$("ptTaskPhase").className = "chip " + (
    t.phase === "completed" ? "ok" : PT_ACTIVE.has(t.phase) ? "run" : "warn");
  pt$("ptTaskMsg").textContent =
    (t.env === "mock" ? "［模拟数据］" : "") +
    (t.error ? String(t.error).slice(0, 120) : `${total} 个商品`);
  pt$("ptTaskBar").style.width = p + "%";
  pt$("ptTaskStat").textContent =
    `进度 ${p}%（${finished}/${total}）`
    + (t.completed ? ` · 完成 ${t.completed}` : "")
    + (t.needs_review ? ` · 待复核 ${t.needs_review}` : "")
    + (t.retryable_failed || t.permanent_failed
      ? ` · 失败 ${(t.retryable_failed || 0) + (t.permanent_failed || 0)}` : "")
    + (t.id ? ` · 任务 ${String(t.id).slice(0, 14)}…` : "");
  pt$("ptPause").disabled = !["running", "created"].includes(t.phase);
  // pausing 窗口也允许点「继续」：后端把意图持久化受理，落定后自动
  // 续跑（响应 pending=true→toast 已接受；绝不静默丢弃用户指令）
  pt$("ptResume").disabled = !["paused", "interrupted", "pausing"].includes(t.phase);
  pt$("ptCancel").disabled = !PT_ACTIVE.has(t.phase);
  pt$("ptRetry").disabled =
    !["completed", "completed_with_issues", "failed", "paused", "interrupted"].includes(t.phase);
}

function ptRenderTaskList(ts) {
  pt$("ptTaskList").innerHTML = (ts || []).slice(0, 8).map((t) =>
    `<span class="pt-taskchip ${PT_ACTIVE.has(t.phase) ? "run" : ""}"
      data-pttask="${esc(t.id)}"
      title="点击查看这个任务的商品结果">${PT_PHASE_LABEL[t.phase] || t.phase}
      ${t.total || 0}个·${esc(fmtMs((Number(t.created_at) || 0) * 1000))}</span>`
  ).join("");
}

async function ptTaskOp(op) {
  if (!PT.taskId) return;
  if (op === "cancel" && !confirm("确定取消这个分类任务？已完成的结果会保留。")) return;
  try {
    busy(true, "操作中…");
    const r = await api(`/api/pt/tasks/${encodeURIComponent(PT.taskId)}/${op}`, {});
    if (r && r.pending) {
      // resume 撞上 pausing 窗口：意图已持久化受理，落定后自动续跑
      toast("已接受：任务正在暂停落定，之后会自动继续");
    } else if (r && !r.applied) {
      toast("未生效：任务状态已变化，已刷新为最新状态");
    }
    await ptTickNow();
  } catch (e) {
    toast(e.message, "err");
  } finally { busy(false); }
}

async function ptTickNow() {
  const r = await api(`/api/pt/tasks/${encodeURIComponent(PT.taskId)}`);
  PT.task = ptMergeTask(r);
  if (PT_ACTIVE.has(PT.task.phase)) ptStartPoll();
  ptRenderTaskCard();
  if (PT.viewing === "task") { await ptLoadTaskItems(); ptRender(); }
}

async function ptLoadTaskItems() {
  if (!PT.taskId) return;
  const r = await api(`/api/pt/tasks/${encodeURIComponent(PT.taskId)}/items`);
  PT.items = r.items || [];
}

/* ---------- 商品列表（A） ---------- */

function ptProducts() {
  const st = PT.status;
  return (S.items || []).filter((it) => {
    const pid = String(it.pid);
    const s = st[pid] || {};
    const task = s.task_state || "";
    switch (PT.filter) {
      case "todo": return s.state !== "classified";
      case "done": return s.state === "classified";
      case "review": return task === "needs_review";
      case "failed": return task === "retryable_failed" || task === "permanent_failed";
      default: return true;
    }
  });
}

function ptFiltered() {
  const q = PT.q.trim().toLowerCase();
  let list = ptProducts();
  if (q) list = list.filter((x) =>
    (String(x.title || "") + String(x.sku || "") + String(x.pid || ""))
      .toLowerCase().includes(q));
  return list;
}

function ptBadge(pid) {
  const s = PT.status[pid] || {};
  const task = s.task_state || "";
  if (task === "needs_review") return `<span class="pt-b rev">👁 待复核</span>`;
  if (task === "retryable_failed") return `<span class="pt-b err">🔁 失败·可重试</span>`;
  if (task === "permanent_failed") return `<span class="pt-b err">❌ 失败</span>`;
  if (s.state === "classified") {
    const by = s.chosen_by === "human" ? "👤 人工确认" : "🤖 AI";
    return `<span class="pt-b ok">${by}</span>`;
  }
  if (task === "running") return `<span class="pt-b run">🔄 分类中</span>`;
  if (task === "pending") return `<span class="pt-b">⚪ 排队</span>`;
  return `<span class="pt-b">—</span>`;
}

function ptRender() {
  ptRenderTabs();
  if (PT.viewing === "task") { ptRenderItems(); return; }
  pt$("ptBackList").classList.add("hidden");
  pt$("ptBatch").classList.remove("hidden");
  pt$("ptSelAll").parentElement.classList.remove("hidden");
  const list = ptFiltered();
  const body = pt$("ptBody");
  pt$("ptEmpty").classList.toggle("hidden", list.length > 0);
  body.innerHTML = list.slice(0, 300).map((it) => {
    const pid = String(it.pid);
    const s = PT.status[pid] || {};
    const ck = PT.sel.has(pid) ? "checked" : "";
    return `<tr data-ptrow="${esc(pid)}">
      <td><input type="checkbox" data-ptsel="${esc(pid)}" ${ck}></td>
      <td><b>${esc(it.title || "（无标题）")}</b><div class="d-imghint">${esc(it.sku || pid)}</div></td>
      <td>${s.pt ? `<b>${esc(s.pt)}</b>` : "—"}${s.state === "classified" ? `<div class="d-imghint">${s.chosen_by === "human" ? "👤 人工确认" : "🤖 AI"}</div>` : ""}</td>
      <td>${ptBadge(pid)}</td>
      <td><button class="btn small" data-ptone="${esc(pid)}">🧭 分类</button></td>
    </tr>`;
  }).join("");
  ptRenderBatchBtn();
}

function ptRenderTabs() {
  pt$("ptFilterTabs").innerHTML = PT_FILTERS.map(([k, label]) =>
    `<div class="tab ${PT.filter === k ? "on" : ""}" data-ptfilter="${k}">${label}</div>`).join("");
}

function ptRenderBatchBtn() {
  pt$("ptBatch").textContent = `🧭 批量 AI 分类（${PT.sel.size}）`;
  pt$("ptBatch").disabled = !PT.sel.size;
}

function ptRenderItems() {
  pt$("ptBackList").classList.remove("hidden");
  pt$("ptBatch").classList.add("hidden");
  pt$("ptSelAll").parentElement.classList.add("hidden");
  ptRenderTabsEmpty();
  const items = PT.items;
  const byPid = {};
  for (const it of (S.items || [])) byPid[String(it.pid)] = it;
  pt$("ptEmpty").classList.toggle("hidden", items.length > 0);
  pt$("ptEmpty").textContent = "任务还没有商品结果（刚创建？稍等片刻点刷新）。";
  pt$("ptBody").innerHTML = items.map((x) => {
    const p = byPid[x.pid] || {};
    const bad = ["needs_review", "retryable_failed", "permanent_failed"].includes(x.state);
    return `<tr data-ptrow="${esc(x.pid)}" class="${bad ? "pt-rowbad" : ""}">
      <td></td>
      <td><b>${esc(p.title || x.pid)}</b><div class="d-imghint">${esc(x.sku || p.sku || x.pid)}</div></td>
      <td>${x.pt ? `<b>${esc(x.pt)}</b>` : "—"}${x.confidence != null ? `<div class="d-imghint">置信度 ${x.confidence}</div>` : ""}</td>
      <td>${PT_ITEM_LABEL[x.state] || esc(x.state)}${x.error ? `<div class="d-imghint pt-err">${esc(String(x.error).slice(0, 90))}</div>` : ""}</td>
      <td><button class="btn small" data-ptopen="${esc(x.pid)}">详情</button></td>
    </tr>`;
  }).join("");
}

function ptRenderTabsEmpty() { pt$("ptFilterTabs").innerHTML = ""; }

/* ---------- 分类详情 / 人工复核（B·D 侧板） ---------- */

async function ptOpenDetail(pid) {
  PT.detailPid = String(pid);
  const box = pt$("ptDetail");
  box.classList.remove("hidden");
  pt$("ptdBody").innerHTML = `<div class="d-imghint">读取中…</div>`;
  try {
    const r = await api(`/api/pt/products/${encodeURIComponent(pid)}`
      + `?store=${encodeURIComponent(PT.store)}&env=${PT.env}`);
    PT.detail = r;
    ptRenderDetail();
  } catch (e) {
    pt$("ptdBody").innerHTML = `<div class="warn">读取失败：${esc(e.message)}</div>`;
  }
}

function ptVerifyBadge(v, env) {
  if (!v) return `<span class="pt-b">未验证</span>`;
  const passed = v.status === "passed";
  if (passed && env === "live") return `<span class="pt-b ok">✅ 官方验证通过（live）</span>`;
  if (passed && env === "mock") return `<span class="pt-b run">🧪 Mock 验证通过（模拟数据）</span>`;
  return `<span class="pt-b warn">未通过（${esc(v.status || "?")}）</span>`;
}

function ptRenderDetail() {
  const d = PT.detail;
  if (!d || String(d.pid) !== String(PT.detailPid)) return;
  const p = (d.product && (d.product.title || d.product.sku))
    ? d.product : byPidTitle(d.pid);
  pt$("ptdTitle").textContent = p.title || d.pid;
  pt$("ptdEnv").innerHTML =
    (d.env === "mock" ? `<span class="pt-mock">模拟数据（不是官方结论）</span> ` : "")
    + `店铺 ${esc(d.store_id)} · 站点 ${esc(d.marketplace_id)} · ${esc(d.pid)}`;
  const a = d.assignment;
  const vs = d.verifications || {};
  const attr = vs.attribute || {};
  const ad = attr.detail || {};
  const cands = d.candidates || [];
  const byPt = {};
  for (const c of cands) byPt[String(c.pt_name).toUpperCase()] = c;
  const nodes = cands.length
    ? [...new Set(cands.flatMap((c) => Object.values(c.evidence || {})
        .flatMap((ev) => ev.classification_ids || [])))] : [];
  const item = d.latest_task_item || {};

  const gapRows = (ad.entries || []).map((e) =>
    `<tr><td>${esc(e.name || e.path)}</td><td>${esc(e.required || "")}</td>
     <td>${esc(e.category || "")}</td><td class="d-imghint">${esc(e.reason || "")}</td></tr>`).join("");

  pt$("ptdBody").innerHTML = `
    <div class="d-sub">当前分类（历史 ${(d.assignment_history || []).length} 条，纠错保留全部记录）</div>
    ${a ? `<div class="pt-asg">
      <b>${esc(a.pt_name)}</b>
      <span class="chip">${a.chosen_by === "human" ? "👤 人工确认" : "🤖 AI"}</span>
      ${a.confidence != null ? `<span class="d-imghint">置信度 ${a.confidence}</span>` : ""}
      <div class="d-imghint">${esc(a.reason || "")}</div>
    </div>` : `<div class="d-imghint">还没有分类结果</div>`}

    <div class="d-sub">官方验证状态</div>
    <div>PT 定义：${ptVerifyBadge(vs.pt_exists, d.env)}
      ${vs.semantic ? `<span class="d-imghint">语义：${esc(vs.semantic.status)}</span>` : ""}</div>
    <div>分类节点：${(vs.browse_node || []).length
      ? vs.browse_node.map((b) => `<span class="pt-b ${b.status === "passed" ? (d.env === "mock" ? "run" : "ok") : "warn"}"
        >BN ${esc(b.node_id || "?")} ${esc(b.status)}</span>`).join(" ")
      : `<span class="pt-b">无节点证据</span>`}</div>
    <div class="d-imghint">人工确认与 Mock 验证都不等于「官方验证通过」。</div>

    <div class="d-sub">Schema 与属性缺口（对照报告，不构成刊登结论）</div>
    <div>Schema：<span class="pt-b">${esc(d.assignment && d.assignment.schema_cache_key ? "已获取" : "—")}</span>
      属性结论：<span class="pt-b ${ad.summary === "incomplete" ? "warn" : ""}">${esc(ad.summary || "—")}</span></div>
    ${gapRows ? `<table class="recycle-table pt-gap"><thead><tr>
      <th>属性</th><th>必填性</th><th>判定</th><th>原因</th></tr></thead>
      <tbody>${gapRows}</tbody></table>` : `<div class="d-imghint">暂无缺口清单（任务没跑到 Schema 阶段）</div>`}

    <div class="d-sub">候选证据（${cands.length} 个 · 只能从中选择）</div>
    <table class="recycle-table"><thead><tr>
      <th>候选 PT</th><th>分数</th><th>证据来源</th></tr></thead><tbody>
      ${cands.map((c) => `<tr>
        <td><b>${esc(c.pt_name)}</b><div class="d-imghint">${esc(c.display_name || "")}</div></td>
        <td>${c.score != null ? esc(c.score) : "—"}</td>
        <td class="d-imghint">${esc(Object.entries(c.evidence || {}).map(([k, ev]) =>
          `${k}: ${(ev.asins || []).slice(0, 2).join(",") || (ev.keywords || []).slice(0, 2).join(",") || (ev.item_name || [""])[0] || ""}`
        ).join("；").slice(0, 160))}</td>
      </tr>`).join("") || `<tr><td colspan="3" class="d-imghint">还没有候选证据（先跑一次分类）</td></tr>`}
    </tbody></table>

    ${item.error ? `<div class="d-sub">复核原因 / 错误</div>
      <div class="warn">${esc(item.error)}（${esc(item.error_kind || "")}）</div>` : ""}

    <div class="d-sub">👤 人工复核（从候选里选一个确认）</div>
    <div class="pt-review">
      ${cands.length ? cands.map((c) => `
        <label class="pt-cand">
          <input type="radio" name="ptPick" value="${esc(c.pt_name)}">
          <b>${esc(c.pt_name)}</b>
          <span class="d-imghint">${esc(c.display_name || "")}</span>
        </label>`).join("") : `<div class="d-imghint">没有候选（先跑分类，或补商品资料）</div>`}
      ${nodes.length ? `<label>分类节点（选填，只能选证据里的）</label>
        <select id="ptdNode" class="inp"><option value="">（不指定）</option>
        ${nodes.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("")}</select>` : ""}
      <input id="ptdReason" class="inp" placeholder="复核备注（可留空）" maxlength="120">
      <button id="ptdConfirm" class="btn primary wide" ${cands.length ? "" : "disabled"}
        >✅ 确认这个分类（人工确认 ≠ 官方验证通过）</button>
    </div>`;
  const btn = pt$("ptdConfirm");
  if (btn) btn.onclick = ptConfirm;
}

function byPidTitle(pid) {
  const it = (S.items || []).find((x) => String(x.pid) === String(pid));
  return it || { title: pid, sku: "" };
}

async function ptConfirm() {
  const pick = document.querySelector('input[name="ptPick"]:checked');
  if (!pick) return toast("先选一个候选", "err");
  const node = pt$("ptdNode") ? pt$("ptdNode").value : "";
  try {
    busy(true, "保存人工确认…");
    const r = await api(`/api/pt/assignments/${encodeURIComponent(PT.detailPid)}/confirm`, {
      store_id: PT.store, env: PT.env,
      pt_name: pick.value, node_id: node || undefined,
      reason: pt$("ptdReason") ? pt$("ptdReason").value.trim() : "",
    });
    toast(r.note || "已记录人工确认", "ok");
    await ptLoadStatus();
    ptRender();
    ptOpenDetail(PT.detailPid);
  } catch (e) {
    toast(e.message, "err");
  } finally { busy(false); }
}

/* ---------- 创建任务（单商品 / 批量） ---------- */

async function ptStart(pids) {
  if (!PT.store) return toast("先选择店铺", "err");
  if (PT.env === "mock" && !confirm("当前是【模拟环境 Mock】——结果全部是模拟数据，仅供演示。继续？")) return;
  try {
    busy(true, "创建分类任务…");
    const r = await api("/api/pt/tasks", { pids, store_id: PT.store, env: PT.env });
    PT.task = r.task || null; PT.taskId = (r.task || {}).id || "";
    PT.viewing = "task";
    PT.sel.clear();   // 选择集随提交消费：防回列表后旧勾选混进新批次
    ptStartPoll();
    await ptTickNow();      // 拉合并进度 + 渲染卡片和条目
    toast(`🧭 任务已创建（${pids.length} 个商品）`, "ok");
  } catch (e) {
    toast(e.message, "err");
  } finally { busy(false); }
}

/* ---------- 事件绑定（一次性） ---------- */

function ptBind() {
  pt$("ptStore").onchange = (e) => { PT.store = e.target.value; ptLoadStatus().then(ptRender); };
  pt$("ptEnv").onchange = (e) => { PT.env = e.target.value; ptRefreshAll(); };
  pt$("ptRefresh").onclick = ptRefreshAll;
  pt$("ptPause").onclick = () => ptTaskOp("pause");
  pt$("ptResume").onclick = () => ptTaskOp("resume");
  pt$("ptCancel").onclick = () => ptTaskOp("cancel");
  pt$("ptRetry").onclick = () => ptTaskOp("retry");
  pt$("ptCloseTask").onclick = () => pt$("ptTaskCard").classList.add("hidden");
  pt$("ptQ").oninput = (e) => { PT.q = e.target.value; ptRender(); };
  pt$("ptBatch").onclick = () => { if (PT.sel.size) ptStart([...PT.sel]); };
  pt$("ptBackList").onclick = async () => {
    PT.viewing = "list"; await ptLoadStatus(); ptRender();
  };
  pt$("ptSelAll").onchange = (e) => {
    for (const x of ptFiltered().slice(0, 300)) {
      if (e.target.checked) PT.sel.add(String(x.pid)); else PT.sel.delete(String(x.pid));
    }
    ptRender();
  };
  pt$("ptdClose").onclick = () => pt$("ptDetail").classList.add("hidden");
  pt$("ptFilterTabs").addEventListener("click", (e) => {
    const t = e.target.closest("[data-ptfilter]");
    if (t) { PT.filter = t.dataset.ptfilter; ptRender(); }
  });
  pt$("ptTaskList").addEventListener("click", (e) => {
    const c = e.target.closest("[data-pttask]");
    if (!c) return;
    PT.taskId = c.dataset.pttask;
    PT.viewing = "task";
    pt$("ptBody").innerHTML = "";
    pt$("ptdBody") && (pt$("ptdBody").innerHTML = "");
    ptTickNow().catch(() => {});
  });
  pt$("ptBody").addEventListener("click", (e) => {
    const one = e.target.closest("[data-ptone]");
    if (one) { ptStart([one.dataset.ptone]); return; }
    const open = e.target.closest("[data-ptopen]");
    if (open) { ptOpenDetail(open.dataset.ptopen); return; }
    const row = e.target.closest("[data-ptrow]");
    if (row && !e.target.closest("input,button")) ptOpenDetail(row.dataset.ptrow);
  });
  pt$("ptBody").addEventListener("change", (e) => {
    const ck = e.target.closest("[data-ptsel]");
    if (!ck) return;
    if (ck.checked) PT.sel.add(ck.dataset.ptsel); else PT.sel.delete(ck.dataset.ptsel);
    ptRenderBatchBtn();
  });
}
