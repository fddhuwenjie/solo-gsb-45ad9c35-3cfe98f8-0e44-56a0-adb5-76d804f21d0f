/* 展馆临时封路导向牌工作台 —— 原生 JavaScript 前端 */
"use strict";

const KIND_CN = { hall: "展厅", entrance: "入口", service: "服务台" };
const STAGE_CN = {
  setup: "布展登记",
  planning: "拟定方案",
  installing: "逐块安装",
  closed: "已封路",
  failed: "封路失败",
};

let state = null;
const $ = (sel) => document.querySelector(sel);
const svgNS = "http://www.w3.org/2000/svg";

function api(path, opts = {}) {
  return fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  }).then(async (r) => {
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(data.message || data.error || `请求失败 (${r.status})`), { data });
    return data;
  });
}

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), 6000);
}

// ------------------------------------------------------------------ 图示
function nodeById(id) { return state.nodes.find((n) => n.id === id); }

function renderMap() {
  const svg = $("#map");
  svg.innerHTML = "";

  // 箭头头部标记
  const defs = document.createElementNS(svgNS, "defs");
  defs.innerHTML = `
    <marker id="arrow-old" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#98a3b0"/>
    </marker>
    <marker id="arrow-new" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="7.5" markerHeight="7.5" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#2f9e44"/>
    </marker>`;
  svg.appendChild(defs);

  // 通道
  for (const c of state.corridors) {
    const a = nodeById(c.a_id), b = nodeById(c.b_id);
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", a.x); line.setAttribute("y1", a.y);
    line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
    if (c.status === "closed") {
      line.setAttribute("stroke", "#c53030");
      line.setAttribute("stroke-dasharray", "8 7");
    } else {
      line.setAttribute("stroke", "#8493a5");
    }
    line.setAttribute("stroke-width", c.status === "closed" ? 3 : 4);
    svg.appendChild(line);
  }

  // 生效箭头（新牌绿色，否则旧牌灰色）；起终点各内缩，避免盖住节点
  for (const n of state.nodes) {
    const e = state.effective[n.id];
    if (!e) continue;
    const target = nodeById(e.points_to);
    const dx = target.x - n.x, dy = target.y - n.y;
    const len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const gap = 30;
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", n.x + ux * gap);
    line.setAttribute("y1", n.y + uy * gap);
    line.setAttribute("x2", target.x - ux * gap);
    line.setAttribute("y2", target.y - uy * gap);
    line.setAttribute("stroke", e.via === "new" ? "#2f9e44" : "#98a3b0");
    line.setAttribute("stroke-width", e.via === "new" ? 3.5 : 2.5);
    line.setAttribute("marker-end", `url(#arrow-${e.via})`);
    svg.appendChild(line);
  }

  // 节点
  const fill = { hall: "#4a6fa5", entrance: "#b7791f", service: "#2f855a" };
  for (const n of state.nodes) {
    const g = document.createElementNS(svgNS, "g");
    const c = document.createElementNS(svgNS, "circle");
    c.setAttribute("cx", n.x); c.setAttribute("cy", n.y);
    c.setAttribute("r", 16);
    c.setAttribute("fill", fill[n.kind]);
    c.setAttribute("stroke", "#fff");
    c.setAttribute("stroke-width", 2);
    g.appendChild(c);
    const label = document.createElementNS(svgNS, "text");
    label.setAttribute("x", n.x);
    label.setAttribute("y", n.y - 24);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("font-size", 14);
    label.setAttribute("font-weight", 700);
    label.setAttribute("fill", "#1f2933");
    label.textContent = n.name;
    g.appendChild(label);
    svg.appendChild(g);
  }
}

// ------------------------------------------------------------------ 核对结果
function renderCheck(boxSel, check) {
  const box = $(boxSel);
  const holder = box.querySelector(".check-result");
  holder.innerHTML = "";
  const v = document.createElement("div");
  v.className = "verdict " + (check.ok ? "ok" : "bad");
  v.textContent = check.ok
    ? "✔ 所有入口均可沿箭头到达服务台"
    : "✘ 首个出错路口：" + check.error.junction_name +
      "（" + { missing: "缺牌", break: "断路", loop: "循环" }[check.error.type] + "）";
  holder.appendChild(v);

  for (const r of check.entrances) {
    const p = document.createElement("div");
    p.className = "path";
    if (r.ok) {
      p.innerHTML = `<span class="entrance-name">${r.entrance_name}</span>：` +
        r.path.map(escapeHtml).join(" → ");
    } else {
      p.innerHTML = `<span class="entrance-name">${r.entrance_name}</span>：` +
        r.path.map(escapeHtml).join(" → ") +
        ` <span class="err">✘ ${escapeHtml(r.error.detail)}</span>`;
    }
    holder.appendChild(p);
  }
  if (!check.entrances.length) {
    const p = document.createElement("div");
    p.className = "path";
    p.textContent = "（尚未登记入口）";
    holder.appendChild(p);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

// ------------------------------------------------------------------ 表单选项
function fillSelect(sel, items, labeler, includeBlank) {
  const cur = sel.value;
  sel.innerHTML = "";
  if (includeBlank) {
    const o = document.createElement("option");
    o.value = ""; o.textContent = "— 请选择 —";
    sel.appendChild(o);
  }
  for (const it of items) {
    const o = document.createElement("option");
    o.value = it.id;
    o.textContent = labeler(it);
    sel.appendChild(o);
  }
  if (cur && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

function neighborsOf(nid) {
  const ids = new Set();
  for (const c of state.corridors) {
    if (c.a_id === nid) ids.add(c.b_id);
    if (c.b_id === nid) ids.add(c.a_id);
  }
  return [...ids].map(nodeById).filter(Boolean);
}

function corridorLabel(c) {
  return `${c.a_name} ↔ ${c.b_name}（${c.status === "closed" ? "已封闭" : "开放"}）`;
}

// ------------------------------------------------------------------ 各面板
function renderLists() {
  const setup = state.stage === "setup";

  // 路口列表
  const ul = $("#list-nodes");
  ul.innerHTML = "";
  for (const n of state.nodes) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="tag ${n.kind}">${KIND_CN[n.kind]}</span>
      <span class="grow">${escapeHtml(n.name)}</span>`;
    if (setup) {
      const x = document.createElement("button");
      x.className = "x"; x.textContent = "删除";
      x.onclick = async () => {
        await api(`/api/nodes/${n.id}`, { method: "DELETE" });
        refresh();
      };
      li.appendChild(x);
    }
    ul.appendChild(li);
  }

  // 通道列表
  const ulc = $("#list-corridors");
  ulc.innerHTML = "";
  for (const c of state.corridors) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="tag ${c.status}">${c.status === "closed" ? "已封闭" : "开放"}</span>
      <span class="grow">${escapeHtml(c.a_name)} ↔ ${escapeHtml(c.b_name)}</span>`;
    if (setup) {
      const x = document.createElement("button");
      x.className = "x"; x.textContent = "删除";
      x.onclick = async () => {
        await api(`/api/corridors/${c.id}`, { method: "DELETE" });
        refresh();
      };
      li.appendChild(x);
    }
    ulc.appendChild(li);
  }

  // 实物牌列表（旧牌 + 新牌分开展示）
  const ulb = $("#list-boards");
  ulb.innerHTML = "";
  const sorted = [...state.boards].sort((a, b) => a.node_id - b.node_id);
  for (const b of sorted) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="tag ${b.kind}">${b.kind === "new" ? "新牌" : "旧牌"}</span>
      <span class="grow">${escapeHtml(b.node_name)} → ${escapeHtml(b.points_to_name)}</span>`;
    if (setup && b.kind === "old") {
      const x = document.createElement("button");
      x.className = "x"; x.textContent = "删除";
      x.onclick = async () => {
        await api(`/api/boards/${b.id}`, { method: "DELETE" });
        refresh();
      };
      li.appendChild(x);
    }
    ulb.appendChild(li);
  }

  // 封路清单
  const editablePlan = state.stage === "setup" || state.stage === "planning";
  const ulcl = $("#list-closures");
  ulcl.innerHTML = "";
  for (const cp of state.closure_plan) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="tag closed">待封</span>
      <span class="grow">${escapeHtml(cp.label)}</span>`;
    if (editablePlan) {
      const x = document.createElement("button");
      x.className = "x"; x.textContent = "移除";
      x.onclick = async () => {
        await api(`/api/plan/closures/${cp.corridor_id}`, { method: "DELETE" });
        refresh();
      };
      li.appendChild(x);
    }
    ulcl.appendChild(li);
  }
  if (!state.closure_plan.length) {
    ulcl.innerHTML = `<li><span class="grow" style="color:#8493a5">（清单为空）</span></li>`;
  }

  // 换牌方案
  const uli = $("#list-items");
  uli.innerHTML = "";
  for (const p of state.plan_items) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="tag ${p.installed ? "new" : "pending"}">${p.installed ? "已装" : "待装"}</span>
      <span class="grow">${escapeHtml(p.node_name)} → ${escapeHtml(p.points_to_name)}</span>`;
    if (editablePlan && !p.installed) {
      const x = document.createElement("button");
      x.className = "x"; x.textContent = "移除";
      x.onclick = async () => {
        await api(`/api/plan/items/${p.id}`, { method: "DELETE" });
        refresh();
      };
      li.appendChild(x);
    }
    uli.appendChild(li);
  }
  if (!state.plan_items.length) {
    uli.innerHTML = `<li><span class="grow" style="color:#8493a5">（方案为空：所有路口沿用旧牌）</span></li>`;
  }

  // 安装按钮
  const ib = $("#install-buttons");
  ib.innerHTML = "";
  for (const p of state.plan_items) {
    const btn = document.createElement("button");
    btn.className = "install-btn" + (p.installed ? " done" : "");
    btn.textContent = (p.installed ? "✔ 已装：" : "安装：") +
      `${p.node_name} → ${p.points_to_name}`;
    btn.disabled = p.installed || state.stage !== "installing";
    if (!p.installed) {
      btn.onclick = async () => {
        try {
          await api(`/api/install/${p.id}`, { method: "POST" });
          toast(`已安装：${p.node_name} → ${p.points_to_name}，现场核对通过`, "success");
        } catch (e) {
          toast(e.message, "error");
        }
        refresh();
      };
    }
    ib.appendChild(btn);
  }
  if (!state.plan_items.length) {
    ib.innerHTML = `<span style="color:#8493a5;font-size:13px">方案无新牌，可直接封路。</span>`;
  }

  // 完成信息
  const done = $("#done-message");
  if (state.stage === "closed") {
    done.innerHTML = `<div class="big-ok">✔ 通道已封闭，所有入口仍可沿实物箭头到达服务台。</div>
      <p class="hint">施工结束后，请在下方「恢复通道 / 拆除新牌」面板逐次恢复，
        全部恢复后自动回到布展登记。</p>`;
  } else if (state.stage === "failed") {
    done.innerHTML = `<div class="big-fail">✘ ${escapeHtml(state.stage_error || "封路失败")}</div>
      <p class="hint">已安装的新牌与仍开放的通道均已保留；上方分别展示方案、实物牌与通道状态。可重置后重新规划。</p>`;
  }
}

function renderStage() {
  const s = state.stage;
  const badge = $("#stage-badge");
  badge.className = "badge " + s;
  badge.textContent = STAGE_CN[s];
  const hints = {
    setup: "登记路口、通道与原有旧牌；全部入口须已能沿旧牌走到服务台。",
    planning: "勾选待封通道、拟定换牌新牌；方案随时可改，开始安装后锁定。",
    installing: "清单与方案已锁定，只能逐块安装；每次安装后自动全量核对。",
    closed: "封路完成，导向正常；施工结束后可逐次开放通道、拆除新牌。",
    failed: "封路失败：新牌与开放通道已保留，作业终止。",
  };
  $("#stage-hint").textContent = hints[s];

  $("#ops-setup").classList.toggle("hidden", s !== "setup");
  $("#ops-plan").classList.toggle("hidden", s !== "planning");
  $("#ops-install").classList.toggle("hidden", s !== "installing");
  $("#ops-done").classList.toggle("hidden", s !== "closed" && s !== "failed");
  $("#ops-restore").classList.toggle("hidden", s !== "closed");
  $("#btn-seed").disabled = s !== "setup";
  $("#btn-close").disabled = s !== "installing";

  const pending = state.plan_items.filter((p) => !p.installed).length;
  if (s === "installing") {
    $("#btn-close").textContent = pending
      ? `封闭通道（还有 ${pending} 块牌未装，暂不可封）`
      : "全部装好，封闭清单中的通道";
  }
}

function renderForms() {
  const s = state.stage;
  const nodeOpts = (it) => `${KIND_CN[it.kind]}｜${it.name}`;

  fillSelect($("#form-corridor [name=a_id]"), state.nodes, nodeOpts, true);
  fillSelect($("#form-corridor [name=b_id]"), state.nodes, nodeOpts, true);

  // 旧牌：路口（非服务台）；指向随路口限定为相邻展厅
  fillSelect($("#form-board [name=node_id]"),
    state.nodes.filter((n) => n.kind !== "service"), nodeOpts, true);
  refreshBoardTargets();

  if (s === "setup" || s === "planning") {
    fillSelect($("#form-closure [name=corridor_id]"),
      state.corridors.filter((c) =>
        !state.closure_plan.some((cp) => cp.corridor_id === c.id)),
      corridorLabel, true);
    fillSelect($("#form-item [name=node_id]"),
      state.nodes.filter((n) => n.kind !== "service"), nodeOpts, true);
    refreshItemTargets();
  }
}

function refreshBoardTargets() {
  const nid = parseInt($("#form-board [name=node_id]").value, 10);
  const sel = $("#form-board [name=points_to]");
  const ns = nid ? neighborsOf(nid) : [];
  fillSelect(sel, ns, (n) => `${KIND_CN[n.kind]}｜${n.name}`, true);
}

function refreshItemTargets() {
  const nid = parseInt($("#form-item [name=node_id]").value, 10);
  const sel = $("#form-item [name=points_to]");
  const ns = nid ? neighborsOf(nid) : [];
  fillSelect(sel, ns, (n) => `${KIND_CN[n.kind]}｜${n.name}`, true);
}

// ------------------------------------------------------------------ 恢复（已封路）
function renderRestore() {
  const sel = $("#form-restore [name=corridor_id]");
  const closed = state.corridors.filter((c) => c.status === "closed");
  fillSelect(sel, closed, corridorLabel, closed.length > 1);

  const box = $("#restore-boards");
  box.innerHTML = "";
  const newBoards = state.boards.filter((b) => b.kind === "new");
  for (const b of newBoards) {
    const label = document.createElement("label");
    label.className = "check-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = b.id;
    label.appendChild(cb);
    const span = document.createElement("span");
    span.textContent = `${b.node_name} → ${b.points_to_name}`;
    label.appendChild(span);
    box.appendChild(label);
  }
  if (!newBoards.length) {
    box.innerHTML = `<span style="color:#8493a5;font-size:13px">（已无新牌可拆）</span>`;
  }

  const hint = $("#restore-final-hint");
  if (closed.length <= 1) {
    const n = newBoards.length;
    hint.textContent = n
      ? `这是最后一条待恢复的已封通道：提交时必须勾选拆完全部 ${n} 块新牌。`
      : "这是最后一条待恢复的已封通道，已无新牌：走查通过后将清空本次方案，回到布展登记。";
    hint.classList.remove("hidden");
  } else {
    hint.textContent = `当前还有 ${closed.length} 条已封通道；本次恢复后可继续选择下一条。`;
    hint.classList.remove("hidden");
  }
}

// ------------------------------------------------------------------ 主刷新
async function refresh() {
  state = await api("/api/state");
  renderStage();
  renderForms();
  renderLists();
  renderRestore();
  renderMap();
  renderCheck("#live-box", state.live_check);
  renderCheck("#sim-box", state.sim_check);
}

// ------------------------------------------------------------------ 事件
function bindForm(formSel, url, bodyFn, okMsg) {
  $(formSel).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    try {
      await api(url, { method: "POST", body: bodyFn(f) });
      toast(okMsg, "success");
      f.reset();
      await refresh();
    } catch (e) {
      toast(e.message, "error");
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  bindForm("#form-node", "/api/nodes",
    (f) => ({ name: f.name.value, kind: f.kind.value }), "路口已登记");
  bindForm("#form-corridor", "/api/corridors",
    (f) => ({ a_id: f.a_id.value, b_id: f.b_id.value }), "通道已登记");
  bindForm("#form-board", "/api/boards",
    (f) => ({ node_id: f.node_id.value, points_to: f.points_to.value }), "旧牌已登记");
  bindForm("#form-closure", "/api/plan/closures",
    (f) => ({ corridor_id: f.corridor_id.value }), "已加入封路清单");
  bindForm("#form-item", "/api/plan/items",
    (f) => ({ node_id: f.node_id.value, points_to: f.points_to.value }), "已加入换牌方案");

  $("#form-board [name=node_id]").addEventListener("change", refreshBoardTargets);
  $("#form-item [name=node_id]").addEventListener("change", refreshItemTargets);

  $("#btn-seed").onclick = async () => {
    try {
      await api("/api/seed", { method: "POST" });
      toast("已载入示例馆区（含封路清单与换牌方案）", "success");
      refresh();
    } catch (e) { toast(e.message, "error"); }
  };

  $("#btn-start").onclick = async () => {
    try {
      await api("/api/plan/start", { method: "POST" });
      toast("方案已确认并锁定，开始逐块安装", "success");
      refresh();
    } catch (e) { toast(e.message, "error"); }
  };

  $("#btn-close").onclick = async () => {
    try {
      await api("/api/close", { method: "POST" });
      toast("✔ 通道已封闭，全部入口导向服务台正常", "success");
      refresh();
    } catch (e) { toast(e.message, "error"); refresh(); }
  };

  $("#btn-reset").onclick = async () => {
    if (!confirm("确定重置本次作业？方案、新牌、封路将全部清空（登记的路口/通道/旧牌保留）。")) return;
    try {
      await api("/api/reset", { method: "POST" });
      toast("作业已重置，回到布展阶段", "success");
      refresh();
    } catch (e) { toast(e.message, "error"); }
  };

  $("#form-restore").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const corridor_id = parseInt(f.corridor_id.value, 10);
    const board_ids = [...f.querySelectorAll('#restore-boards input[type=checkbox]:checked')]
      .map((cb) => parseInt(cb.value, 10));
    if (!corridor_id) {
      toast("请先选择一条要开放的已封通道", "error");
      return;
    }
    try {
      const out = await api("/api/restore", {
        method: "POST",
        body: { corridor_id, board_ids },
      });
      toast(out.final
        ? "✔ 最后一条通道已开放、新牌全部拆完，方案已清空，回到布展登记"
        : `✔ 通道已开放，拆除 ${board_ids.length} 块新牌，现场走查通过；可继续恢复下一条`,
        "success");
      refresh();
    } catch (e) {
      toast(e.message, "error");
      refresh();
    }
  });

  refresh().catch((e) => toast("加载失败：" + e.message, "error"));
});
