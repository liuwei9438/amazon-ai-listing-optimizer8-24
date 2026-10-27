/* ptw.js —— 🧪 Mock 刊登台（S2e：S2c 刊登任务中心 + S2d 异常复核队列 UI）
   独立文件（同 pt.js 范式：不改 app.js 既有逻辑，app.js 只挂 enter 与入口）。
   铁律（验收令六）：
   - 本视图一切结果都是 Mock 模拟测试：恒显「模拟测试」徽章，绝不显示为
     亚马逊真实接受；「本地校验通过」也不等于平台接受。
   - 人工处置只调既有授权 API（resolve/attr_review/attr_edit），操作人=
     会话身份（服务端推导），前端绝无 human 参数伪造。
   - 处置只是补证据/记录：修复后必须重跑校验，不允许一键「解决」绕过。
   状态真源=服务器 SQLite：刷新/重开页面从 /api/pt/* 恢复任务与队列。 */
"use strict";

const PTW = {
  inited: false, stores: [], store: "",
  tab: "tasks",                 // tasks | queue
  tasks: [], taskId: "", task: null, items: [],
  poll: null, seq: 0,           // 任务轮询 + 竞态序号（切换任务/店铺作废旧响应）
  queue: [], qBusy: false,      // 处置在飞守卫（重复点击不重复执行）
  qType: "", qStatus: "open", qPid: "",
};
window.PTW = PTW;   // 挂 window（const 不上 window 属性——外部探针/测试可观测）

const PTW_PHASE = {
  created: "⚪ 已创建", running: "🔄 进行中", pausing: "⏸ 暂停中…",
  paused: "⏸ 已暂停", completed: "✅ 已完成",
  completed_with_issues: "⚠️ 部分成功（有阻断/失败）",
  failed: "❌ 失败", interrupted: "🔌 中断（可续跑）",
  cancelling: "⛔ 取消中…", cancelled: "⛔ 已取消",
};
const PTW_ACTIVE = new Set(["created", "running", "pausing", "cancelling"]);

const PTW_ITEM = {
  pending: "⚪ 排队", running: "🔄 处理中", completed: "✅ 模拟提交完成",
  blocked: "🛑 阻断（待复核修复）", retryable_failed: "🔁 可重试失败",
  permanent_failed: "❌ 失败", cancelled: "⛔ 已取消",
};
/* S3-B2 平台结果契约视图（后端 platform 字段；一切 Mock 标注恒在，
   unknown=已受理但结果未知，绝不显示成平台真实接受） */
const PTW_PLATFORM = {
  not_submitted: "—（未提交）",
  submitting: "🎭 提交中（模拟）",
  acknowledged: "🎭 已受理（模拟受理号）",
  processing: "🎭 平台处理中（模拟）",
  accepted: "🎭 模拟已接受（非官方结论）",
  rejected: "🎭 模拟被拒（非官方结论）",
  partially_accepted: "🎭 模拟部分接受",
  unknown: "❓ 结果未知（已受理不重发）",
};
const PTW_EXC = {
  missing_required: ["缺失必填", "ptw-x-miss"],
  value_conflict: ["值冲突", "ptw-x-conf"],
  insufficient_evidence: ["证据不足", "ptw-x-ev"],
  schema_uncertain: ["Schema 不确定", "ptw-x-unc"],
  classification_review: ["分类需复核", "ptw-x-cls"],
  variant_incomplete: ["变体不完整", "ptw-x-var"],
  context_stale: ["上下文已变", "ptw-x-stale"],
};
const PTW_ACTIONS = [
  ["confirm", "✅ 确认值"], ["edit", "✏️ 改值"],
  ["dismiss", "🗑 驳回留档"], ["reclassify", "🔁 记录改判"],
];

function ptw$(id) { return document.getElementById(id); }

window.PtwUI = {
  enter() {
    if (!PTW.inited) { ptwBind(); ptwFillTypes(); PTW.inited = true; }
    ptwLoadStores().then(() => ptwRefresh());
  },

  /* ---- 三入口（single/selected/category 全走同一 S2c 引擎）---- */
  async startSingle(pid, storeId) {
    return ptwCreate({
      pids: [String(pid)], source_mode: "single", store_id: storeId || "",
      from: `单品（详情工作台）`,
    });
  },
  async startSelected(pids) {
    return ptwCreate({
      pids: pids.map(String), source_mode: "selected", store_id: "",
      from: `勾选 ${pids.length} 个商品`,
    });
  },
  async startCategory(cat) {
    if (!cat) { toast("「未分类」不是分类——先到分类树里选一个真分类", "err"); return; }
    return ptwCreate({
      category: String(cat), source_mode: "category", store_id: "",
      from: `分类「${cat}」`,
    });
  },

  /* ---- 详情工作台（app.js renderWb）共用的动作助手 ---- */
  async revalidate(pid, storeId, env) {
    try {
      const q = `?env=${encodeURIComponent(env || "mock")}`
        + (storeId ? `&store=${encodeURIComponent(storeId)}` : "");
      const r = await api(`/api/pt/validation/${encodeURIComponent(pid)}${q}`);
      const rep = r.report || {};
      toast(r.validated
        ? "✅ 本地校验通过（≠ 亚马逊平台最终接受）"
        : `🛑 仍有阻断 ${rep.blocking || "?"} 项——见异常工单`, r.validated ? "ok" : "");
      return r;
    } catch (e) {
      toast(e.message, "err");
      throw e;
    }
  },
  attrAction(pid, storeId, env, path, action, value) {
    const body = { env: env || "mock", action, attribute_path: path };
    if (storeId) body.store_id = storeId;
    if (value !== undefined) body.value = value;
    const url = action === "edit"
      ? `/api/pt/attributes/${encodeURIComponent(pid)}/edit`
      : `/api/pt/attributes/${encodeURIComponent(pid)}/review`;
    return ptwDoAttr(url, body, action);
  },
};

/* ---------- 初始化：店铺 / 类型下拉 ---------- */

async function ptwLoadStores() {
  try {
    const r = await api("/api/pt/overview");
    PTW.stores = r.stores || [];
    if (!PTW.store) PTW.store = r.active_store || (PTW.stores[0] || {}).id || "";
  } catch (e) { /* 顶栏已示 pt 状态；这里保持空店铺 */ }
  const sel = ptw$("ptwStore");
  sel.innerHTML = PTW.stores.map((s) =>
    `<option value="${esc(s.id)}">${esc(s.name || s.id)}（${esc(s.marketplace)}）</option>`).join("");
  sel.value = PTW.store;
}

function ptwFillTypes() {
  ptw$("ptwType").innerHTML = `<option value="">全部异常类型</option>`
    + Object.entries(PTW_EXC).map(([k, [label]]) =>
      `<option value="${esc(k)}">${esc(label)}（${esc(k)}）</option>`).join("");
}

function ptwRefresh() {
  if (PTW.tab === "tasks") ptwLoadTasks(); else ptwLoadQueue();
}

/* ---------- 任务中心（S2c 引擎回读，只经既有 API） ---------- */

async function ptwLoadTasks() {
  try {
    const r = await api("/api/pt/listings/tasks?limit=30");
    PTW.tasks = (r.tasks || []).filter(
      (t) => !PTW.store || t.store_id === PTW.store);
  } catch (e) { PTW.tasks = []; toast(e.message, "err"); }
  ptwRenderTaskChips();
  const act = PTW.tasks.find((t) => PTW_ACTIVE.has(t.phase)) || PTW.tasks[0];
  if (act) { await ptwSelectTask(act.id, { auto: true }); }
  else { PTW.taskId = ""; PTW.task = null; PTW.items = []; ptwRenderTask(); }
}

async function ptwSelectTask(id, opts = {}) {
  PTW.taskId = String(id);
  await ptwTickTask();
  if (PTW_ACTIVE.has((PTW.task || {}).phase || "")) ptwStartPoll();
}

async function ptwTickTask() {
  const id = PTW.taskId;
  if (!id) return;
  try {
    const [d, it] = await Promise.all([
      api(`/api/pt/listings/tasks/${encodeURIComponent(id)}`),
      api(`/api/pt/listings/tasks/${encodeURIComponent(id)}/items`),
    ]);
    if (PTW.taskId !== id) return;             // 已切到别的任务：旧响应丢弃
    PTW.task = Object.assign({}, d.task || {}, d.progress || {});
    PTW.items = it.items || [];
    if (!PTW_ACTIVE.has(d.task.phase) && PTW.poll) {
      ptwStopPoll();
      toast("🧪 模拟刊登任务已结束：" + (PTW_PHASE[d.task.phase] || d.task.phase)
        + "（模拟结果，非亚马逊官方结论）");
    }
  } catch (e) { ptwStopPoll(); if (PTW.tab === "tasks") toast(e.message, "err"); }
  ptwRenderTask();
}

function ptwStartPoll() { ptwStopPoll(); PTW.poll = setInterval(ptwTickTask, 2000); }
function ptwStopPoll() { if (PTW.poll) { clearInterval(PTW.poll); PTW.poll = null; } }

function ptwRenderTaskChips() {
  ptw$("ptwTaskList").innerHTML = (PTW.tasks || []).slice(0, 12).map((t) =>
    `<span class="pt-taskchip ${PTW_ACTIVE.has(t.phase) ? "run" : ""}"
      data-ptwtask="${esc(t.id)}"
      title="${esc(t.source_mode || "")}${t.source_ref ? " · " + esc(t.source_ref) : ""}"
      >${PTW_PHASE[t.phase] || t.phase} ${t.total || 0}个·${esc(fmtMs((Number(t.created_at) || 0) * 1000))}</span>`
  ).join("");
}

function ptwRenderTask() {
  const card = ptw$("ptwTaskCard");
  const t = PTW.task;
  ptw$("ptwTaskEmpty").classList.toggle("hidden", !!t);
  if (!t) { card.classList.add("hidden"); ptw$("ptwItemsBody").innerHTML = ""; return; }
  card.classList.remove("hidden");
  const total = Number(t.total) || 0;
  const done = (Number(t.completed) || 0) + (Number(t.blocked) || 0)
    + (Number(t.retryable_failed) || 0) + (Number(t.permanent_failed) || 0)
    + (Number(t.cancelled) || 0);
  const p = total ? Math.round(done * 100 / total) : 0;
  const phaseEl = ptw$("ptwTaskPhase");
  phaseEl.textContent = PTW_PHASE[t.phase] || t.phase;
  phaseEl.className = "chip " + (t.phase === "completed" ? "ok"
    : t.phase === "completed_with_issues" ? "warn"
    : PTW_ACTIVE.has(t.phase) ? "run" : "warn");
  ptw$("ptwTaskMsg").textContent =
    `🎭 模拟测试 · ${t.source_mode || "?"}${t.source_ref ? ` · ${t.source_ref}` : ""}`
    + (t.error ? ` · ${String(t.error).slice(0, 90)}` : "");
  ptw$("ptwTaskBar").style.width = p + "%";
  const c = t;
  ptw$("ptwTaskStat").textContent =
    `进度 ${p}%（${done}/${total}）`
    + ` · ✅ 完成 ${c.completed || 0}`
    + (c.blocked ? ` · 🛑 阻断 ${c.blocked}` : "")
    + ((c.retryable_failed || c.permanent_failed)
      ? ` · ❌ 失败 ${(c.retryable_failed || 0) + (c.permanent_failed || 0)}` : "")
    + (c.cancelled ? ` · ⛔ 取消 ${c.cancelled}` : "")
    + ` · 任务 ${String(t.id).slice(0, 14)}…`
    + (t.phase === "completed_with_issues"
      ? "（部分成功——阻断/失败的商品没提交，不是全部成功）" : "");
  ptw$("ptwPause").disabled = !["running", "created"].includes(t.phase);
  ptw$("ptwResume").disabled = !["paused", "interrupted", "pausing"].includes(t.phase);
  ptw$("ptwCancel").disabled = !PTW_ACTIVE.has(t.phase);
  ptw$("ptwRetry").disabled =
    !["completed", "completed_with_issues", "failed", "paused", "interrupted"].includes(t.phase);
  ptw$("ptwNewRound").disabled =
    !["completed", "completed_with_issues", "failed", "cancelled"].includes(t.phase);

  const byPid = {};
  for (const it of (S.items || [])) byPid[String(it.pid)] = it;
  ptw$("ptwItemsBody").innerHTML = (PTW.items || []).map((x) => {
    const prod = byPid[String(x.pid)] || {};
    const pv = x.platform || {};
    let sub = "—（还没走到提交）";
    if (pv.status && pv.status !== "not_submitted") {
      sub = PTW_PLATFORM[pv.status] || esc(pv.status);
      if (x.submission_id)
        sub += `<div class="d-imghint">受理 ${esc(x.submission_id)}（模拟受理号）</div>`;
      if (Array.isArray(pv.issues) && pv.issues.length)
        sub += `<div class="d-imghint">issues ${esc(pv.issues.map((i) => i.code).join("/"))}</div>`;
    } else if (x.submission_id) {
      sub = x.submit_status === "PROCESSED"
        ? `🎭 模拟已处理（${esc(x.submit_status)}）`
        : x.submit_status === "REJECTED"
          ? `🎭 模拟被拒（${esc(x.submit_status)}）`
          : `🎭 模拟已受理·处理中（${esc(x.submit_status || "?")}）`;
      sub += `<div class="d-imghint">受理 ${esc(x.submission_id)}（模拟受理号）</div>`;
    }
    const bad = ["blocked", "retryable_failed", "permanent_failed"].includes(x.state);
    return `<tr class="${bad ? "pt-rowbad" : ""}" data-ptwitem="${esc(x.pid)}:${esc(x.sku)}">
      <td><b>${esc(prod.title || x.pid)}</b><div class="d-imghint">pid ${esc(x.pid)} · SKU ${esc(x.sku)}</div></td>
      <td>${esc(x.variant_role || "—")}${x.parent_sku ? `<div class="d-imghint">父 ${esc(x.parent_sku)}</div>` : ""}</td>
      <td>${PTW_ITEM[x.state] || esc(x.state)}</td>
      <td>${esc(x.stage || "—")}</td>
      <td class="d-imghint">${esc(String(x.error || "").slice(0, 110))
        || (x.error_kind ? esc(x.error_kind) : "—")}</td>
      <td class="d-imghint">${sub}</td>
    </tr>`;
  }).join("");
}

async function ptwTaskOp(op) {
  if (!PTW.taskId) return;
  if (op === "cancel" && !confirm("确定取消这个模拟刊登任务？已受理的模拟提交不会重发。")) return;
  if (op === "retry" && !confirm("把阻断/失败的商品拉回重试？（已受理的模拟提交绝不动）")) return;
  try {
    busy(true, "操作中…");
    await api(`/api/pt/listings/tasks/${encodeURIComponent(PTW.taskId)}/${op}`, {});
    await ptwTickTask();
  } catch (e) {
    toast(e.message, "err");
  } finally { busy(false); }
}

/* S3-B2：显式新一轮刊登（new_operation=true——旧任务与快照原样保留，
   不复用也不覆盖历史；同内容重放仍走创建入口的幂等 replay） */
async function ptwNewRound() {
  const t = PTW.task;
  if (!t || !t.id) return;
  const payload = { env: "mock", store_id: t.store_id,
    source_mode: t.source_mode, new_operation: true,
    from: "新一轮刊登（旧任务与快照保留）" };
  if (t.source_mode === "category" && t.source_ref) {
    payload.category = t.source_ref;
  } else {
    const pids = [...new Set((PTW.items || []).map((x) => String(x.pid)))];
    if (!pids.length) { toast("没有商品可以重发一轮", "err"); return; }
    payload.pids = pids;
  }
  await ptwCreate(payload);
}

/* ---------- 三入口共用创建（全部 source_mode 明示 + env=mock 硬编码） ---------- */

async function ptwCreate(payload) {
  if (!confirm(
    `发起【模拟刊登任务】（${payload.from}）？\n`
    + "这是 Mock 模拟测试：分类→属性校验→模拟提交全程演示，"
    + "绝不调用真实亚马逊 Listings 接口，也不走员工真实上传通道。")) return;
  try {
    busy(true, "创建模拟刊登任务…");
    const r = await api("/api/pt/listings/tasks",
      Object.assign({ env: "mock" }, payload));
    const t = r.task || {};
    toast(`🧪 模拟刊登任务已创建`
      + (t._created === false
        ? (t._verdict === "replay"
          ? "——同内容重试：复用旧任务（未重新提交）"
          : "——同 SKU 集合已有任务，复用旧任务") : ""), "ok");
    setView("ptw");
    PTW.tab = "tasks";
    ptwShowTab();
    PTW.taskId = t.id;
    await ptwLoadTasks();
    ptwStartPoll();
    await ptwTickTask();
  } catch (e) {
    const msg = String(e.message || "");
    if (!payload.new_operation
      && (msg.includes("new_operation") || msg.includes("已结束"))) {
      // S3-B2 幂等新鲜度：同键旧任务已终态且内容已变——服务端 409
      // stale_task 拒绝静默复用；由用户显式决定是否发起新一轮
      toast("旧任务已结束且商品内容有变化，不会静默复用（stale_task）",
        "err");
      if (confirm("同参数的旧任务已结束、商品内容已有变化。\n"
        + "要显式发起新一轮刊登吗？（旧任务与快照原样保留）")) {
        return ptwCreate(Object.assign({}, payload, {
          new_operation: true,
          from: "新一轮刊登（旧任务与快照保留）" }));
      }
      return;
    }
    toast(e.message, "err");
  } finally { busy(false); }
}

/* ---------- 异常复核队列（S2d，处置=人工授权 API） ---------- */

async function ptwLoadQueue() {
  const q = [`env=mock`, `status=${encodeURIComponent(PTW.qStatus || "open")}`,
             `limit=200`];
  if (PTW.store) q.push(`store=${encodeURIComponent(PTW.store)}`);
  if (PTW.qType) q.push(`type=${encodeURIComponent(PTW.qType)}`);
  if (PTW.qPid.trim()) q.push(`pid=${encodeURIComponent(PTW.qPid.trim())}`);
  try {
    const r = await api("/api/pt/review/exceptions?" + q.join("&"));
    PTW.queue = r.exceptions || [];
  } catch (e) {
    PTW.queue = [];
    toast(e.message, "err");
  }
  ptwRenderQueue();
}

function ptwExcRows() {
  const byPid = {};
  for (const it of (S.items || [])) byPid[String(it.pid)] = it;
  return (PTW.queue || []).map((x) => {
    const [label, cls] = PTW_EXC[x.exception_type] || [x.exception_type, ""];
    const det = x.detail || {};
    const ent = (det.entries || [])[0] || {};
    const why = ent.reason || det.summary || "—";
    const how = ent.suggestion || "";
    const prod = byPid[String(x.pid)] || {};
    let stCell = `<span class="pt-b run">待处理</span>`;
    if (x.status === "resolved") {
      stCell = `<span class="pt-b ok">👤 已人工处置</span>
        <div class="d-imghint">${esc(x.resolution_action || "?")} · ${esc(x.resolved_by || "?")}
        · ${esc(fmtMs((Number(x.resolved_at) || 0) * 1000))}</div>
        ${x.original_value != null || x.resolved_value != null
          ? `<div class="d-imghint">原值 ${esc(JSON.stringify(x.original_value))} → 新值 ${esc(JSON.stringify(x.resolved_value))}</div>` : ""}
        ${x.resolution_note ? `<div class="d-imghint">备注：${esc(x.resolution_note)}</div>` : ""}`;
    } else if (x.status === "auto_superseded") {
      stCell = `<span class="pt-b">🔁 引擎自动关单（重校验后已消解）</span>`;
    }
    const open = x.status === "open";
    const acts = open
      ? PTW_ACTIONS.map(([a, label2]) =>
          `<button class="btn small" data-ptwact="${esc(a)}" data-ptwxid="${esc(x.id)}"
            ${PTW.qBusy ? "disabled" : ""}>${label2}</button>`).join(" ")
        + `<input class="inp ptw-qedit hidden" data-ptwxid="${esc(x.id)}"
            placeholder="改值用：输入新值后点「改值」"
            style="max-width:170px;margin-top:4px">`
      : `<span class="d-imghint">${x.status === "resolved"
          ? "处置已审计留档（历史事实，不可重写）" : ""}</span>`;
    return `<tr data-ptwexc="${esc(x.id)}" class="${open ? "" : "pt-aidimrow"}">
      <td><span class="pt-b ${cls}">${esc(label)}</span>
        <div class="d-imghint">${esc(x.attribute_path || "—")}</div></td>
      <td class="d-imghint">${esc(prod.title || x.pid)}<div>${esc(x.pid)}</div></td>
      <td><div>${esc(String(why).slice(0, 120))}</div>
        ${how ? `<div class="d-imghint">建议：${esc(String(how).slice(0, 110))}</div>` : ""}
        <div class="d-imghint">来源 ${esc(x.source || "—")} · ${esc(fmtMs((Number(x.updated_at) || 0) * 1000))}</div></td>
      <td>${stCell}</td>
      <td>${acts}</td>
    </tr>`;
  }).join("");
}

function ptwRenderQueue() {
  const rows = ptwExcRows();
  ptw$("ptwQueueBody").innerHTML = rows;
  ptw$("ptwQueueEmpty").classList.toggle("hidden", !!rows);
  ptw$("ptwQueueCount").textContent =
    `当前过滤：${(PTW.queue || []).length} 条（状态=${PTW.qStatus || "open"}`
    + `${PTW.qType ? ` · 类型=${PTW.qType}` : ""}${PTW.qPid.trim() ? ` · pid=${PTW.qPid.trim()}` : ""}）`
    + `——处置走人工授权接口并全程审计；处置后须重跑校验。`;
}

async function ptwResolve(xid, action) {
  if (PTW.qBusy) return;                       // 双击/重试守卫：在飞即拒
  const row = (PTW.queue || []).find((x) => String(x.id) === String(xid));
  if (!row || row.status !== "open") {
    toast("该工单已不是待处理状态（可能是重复点击）——已刷新", "");
    ptwLoadQueue();
    return;
  }
  let value;
  if (action === "edit") {
    const inp = document.querySelector(
      `.ptw-qedit[data-ptwxid="${CSS.escape(String(xid))}"]`);
    const raw = inp ? inp.value.trim() : "";
    if (!raw) {
      if (inp) inp.classList.remove("hidden");
      toast("改值需要填新值（输入框在本行）——补值后处置，不能空解决", "err");
      return;
    }
    value = raw;
  }
  if (action === "dismiss" && !confirm(
    "驳回留档：仅记录人工判定「此异常不处理」，不改属性值。\n"
    + "确认？（缺失的属性不会因此补上——重跑校验仍会阻断）")) return;
  PTW.qBusy = true;
  ptwRenderQueue();                            // 立即禁用全部按钮（防连点）
  try {
    const body = { action };
    if (value !== undefined) body.value = value;
    const r = await api(`/api/pt/review/exceptions/${encodeURIComponent(xid)}/resolve`, body);
    toast(`✅ 已处置（${action}，操作人=当前账号，已审计）`
      + "——请重跑校验确认全链", "ok");
  } catch (e) {
    const msg = String(e.message || "");
    toast(/已处置|状态|409/.test(msg) && msg.indexOf("重复") < 0
      ? `该工单刚被处置过（重复点击不重复执行）：${msg}` : msg, "err");
  } finally {
    PTW.qBusy = false;
    await ptwLoadQueue();
  }
}

async function ptwDoAttr(url, body, action) {
  try {
    busy(true, "提交人工操作…");
    const r = await api(url, body);
    toast(action === "edit" ? "✏️ 已人工改值（新修订，已审计）"
      : `✅ 已${action === "confirm" ? "确认" : "否决"}（人工操作，已审计）`, "ok");
    return r;
  } catch (e) {
    const msg = String(e.message || "");
    toast(/复核状态不允许|没有该属性/.test(msg) ? `该属性刚被操作过：${msg}` : msg, "err");
    throw e;
  } finally { busy(false); }
}

/* ---------- 视图切换 / 事件绑定 ---------- */

function ptwShowTab() {
  ptw$("ptwTabs").querySelectorAll(".tab").forEach((el) =>
    el.classList.toggle("on", el.dataset.ptwtab === PTW.tab));
  ptw$("ptwTaskPane").classList.toggle("hidden", PTW.tab !== "tasks");
  ptw$("ptwQueuePane").classList.toggle("hidden", PTW.tab !== "queue");
  if (PTW.tab === "tasks" && PTW.taskId && PTW_ACTIVE.has((PTW.task || {}).phase || "")) {
    ptwStartPoll();
  }
}

function ptwBind() {
  ptw$("ptwTabs").addEventListener("click", (e) => {
    const t = e.target.closest("[data-ptwtab]");
    if (!t) return;
    PTW.tab = t.dataset.ptwtab;
    ptwShowTab();
    ptwRefresh();
  });
  ptw$("ptwRefresh").onclick = () => ptwRefresh();
  ptw$("ptwStore").onchange = (e) => {
    PTW.store = e.target.value;
    PTW.taskId = "";
    ptwStopPoll();
    ptwRefresh();
  };
  ptw$("ptwPause").onclick = () => ptwTaskOp("pause");
  ptw$("ptwResume").onclick = () => ptwTaskOp("resume");
  ptw$("ptwCancel").onclick = () => ptwTaskOp("cancel");
  ptw$("ptwRetry").onclick = () => ptwTaskOp("retry");
  ptw$("ptwNewRound").onclick = () => ptwNewRound();
  ptw$("ptwCloseTask").onclick = () => ptw$("ptwTaskCard").classList.add("hidden");
  ptw$("ptwTaskList").addEventListener("click", (e) => {
    const c = e.target.closest("[data-ptwtask]");
    if (!c) return;
    ptwSelectTask(c.dataset.ptwtask).catch(() => {});
  });
  ptw$("ptwFilter").onclick = () => {
    PTW.qType = ptw$("ptwType").value;
    PTW.qStatus = ptw$("ptwStatus").value;
    PTW.qPid = ptw$("ptwPid").value;
    ptwLoadQueue();
  };
  ptw$("ptwQueueBody").addEventListener("click", (e) => {
    const b = e.target.closest("[data-ptwact]");
    if (!b) return;
    if (b.dataset.ptwact === "edit") {
      const inp = document.querySelector(
        `.ptw-qedit[data-ptwxid="${CSS.escape(b.dataset.ptwxid)}"]`);
      if (inp) inp.classList.remove("hidden");   // 先亮输入框；有值才真正提交
    }
    ptwResolve(b.dataset.ptwxid, b.dataset.ptwact);
  });
  ptw$("ptwItemsBody").addEventListener("click", (e) => {
    const row = e.target.closest("[data-ptwitem]");
    if (row && !e.target.closest("button")) {
      const pid = String(row.dataset.ptwitem).split(":")[0];
      setView("list");
      setTimeout(() => { try { openDetail(pid); } catch (err) { /* 列表没渲染好就算了 */ } }, 400);
    }
  });
}
