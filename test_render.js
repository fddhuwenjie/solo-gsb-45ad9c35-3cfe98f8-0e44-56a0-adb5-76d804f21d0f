// 前端渲染冒烟测试：用极简 DOM 垫片加载 app.js 的渲染逻辑（非交付物）。
const fs = require("fs");

function makeEl(tag, cache) {
  const el = {
    tagName: tag, children: [], attributes: {}, style: {}, dataset: {},
    _cls: new Set(), _text: "", _html: "",
    classList: {
      add(c) { el._cls.add(c); }, remove(c) { el._cls.delete(c); },
      toggle(c, force) { force ? el._cls.add(c) : el._cls.delete(c); },
      contains(c) { return el._cls.has(c); },
    },
    set className(v) { el._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get className() { return [...el._cls].join(" "); },
    set textContent(v) { el._text = String(v); },
    get textContent() { return el._text; },
    set innerHTML(v) { el._html = String(v); el.children = []; },
    get innerHTML() { return el._html; },
    setAttribute(k, v) { el.attributes[k] = String(v); },
    getAttribute(k) { return el.attributes[k]; },
    appendChild(c) { el.children.push(c); c.parentNode = el; return c; },
    append(...cs) { cs.forEach((c) => el.appendChild(c)); },
    remove() {
      if (el.parentNode) el.parentNode.children = el.parentNode.children.filter((x) => x !== el);
    },
    _sub: {},
    querySelector(sel) {
      if (!el._sub[sel]) el._sub[sel] = makeEl("div");
      return el._sub[sel];
    },
    querySelectorAll() { return []; },
    addEventListener() {},
    reset() { el.value = ""; },
    value: "", options: [], disabled: false,
  };
  return el;
}

const elements = {};
global.document = {
  createElementNS: () => makeEl("svg-el"),
  createElement: (t) => makeEl(t),
  querySelector(sel) {
    if (!elements[sel]) elements[sel] = makeEl("div");
    return elements[sel];
  },
  addEventListener() {},
};
global.fetch = async () => { throw new Error("no fetch in smoke test"); };
global.confirm = () => true;

const code = fs.readFileSync("static/app.js", "utf8");
// 去掉末尾自动启动 refresh 的调用，改为手动注入 state
const patched = code.replace(
  /document\.addEventListener\("DOMContentLoaded"[\s\S]*?\}\);\s*$/,
  ""
) + "\nglobalThis.__api = { renderStage, renderForms, renderLists, renderMap, renderCheck, renderReopen, set state(v){state=v}, get state(){return state} };";
const runGlobal = (0, eval);
runGlobal(patched);
const R = globalThis.__api;

let failures = 0;
const ok = (c, m) => { console.log((c ? "  ✔ " : "  ✘ ") + m); if (!c) failures++; };

function makeState(stage) {
  return {
    stage, stage_error: stage === "failed" ? "封路失败：test" : null,
    nodes: [
      { id: 1, name: "入口1", kind: "entrance", x: 80, y: 120 },
      { id: 3, name: "A展厅", kind: "hall", x: 320, y: 120 },
      { id: 4, name: "B展厅", kind: "hall", x: 320, y: 360 },
      { id: 5, name: "C展厅", kind: "hall", x: 560, y: 240 },
      { id: 6, name: "服务台", kind: "service", x: 800, y: 240 },
    ],
    corridors: [
      { id: 1, a_id: 1, b_id: 3, status: "open", a_name: "入口1", b_name: "A展厅" },
      { id: 4, a_id: 3, b_id: 4, status: "open", a_name: "A展厅", b_name: "B展厅" },
      { id: 5, a_id: 4, b_id: 5, status: stage === "closed" ? "closed" : "open",
        a_name: "B展厅", b_name: "C展厅" },
    ],
    boards: [
      { id: 1, node_id: 1, kind: "old", points_to: 3, node_name: "入口1", points_to_name: "A展厅" },
      { id: 9, node_id: 3, kind: "new", points_to: 5, node_name: "A展厅", points_to_name: "C展厅" },
    ],
    closure_plan: stage === "setup" ? [] :
      [{ corridor_id: 5, a_id: 4, b_id: 5, status: "open", label: "B展厅 ↔ C展厅（开放）" }],
    plan_items: [
      { id: 1, node_id: 3, points_to: 5, node_name: "A展厅", points_to_name: "C展厅", installed: 1 },
      { id: 2, node_id: 4, points_to: 3, node_name: "B展厅", points_to_name: "A展厅", installed: 0 },
    ],
    effective: { 1: { via: "old", points_to: 3 }, 3: { via: "new", points_to: 5 } },
    live_check: {
      ok: false,
      error: { type: "missing", junction: 4, junction_name: "B展厅", trail: [1, 3, 4], detail: "路口「B展厅」缺牌" },
      entrances: [
        { entrance_id: 1, entrance_name: "入口1", ok: false,
          path: ["入口1", "A展厅", "B展厅"],
          error: { type: "missing", junction_name: "B展厅", detail: "路口「B展厅」缺牌", trail: [1, 3, 4] } },
      ],
    },
    sim_check: { ok: true, error: null, entrances: [
      { entrance_id: 1, entrance_name: "入口1", ok: true, path: ["入口1", "A展厅", "C展厅"] },
    ] },
    reopen_check: { ok: true, error: null, entrances: [
      { entrance_id: 1, entrance_name: "入口1", ok: true, path: ["入口1", "A展厅", "B展厅", "C展厅"] },
    ] },
  };
}

for (const stage of ["setup", "planning", "installing", "closed", "failed"]) {
  console.log("== 阶段 " + stage + " ==");
  R.state = makeState(stage);
  try {
    R.renderStage();
    R.renderForms();
    R.renderLists();
    R.renderReopen();
    R.renderMap();
    R.renderCheck("#live-box", R.state.live_check);
    R.renderCheck("#sim-box", R.state.sim_check);
    ok(true, "六个渲染函数无异常");
    // 只有安装阶段显示安装面板
    const installCard = document.querySelector("#ops-install");
    ok(installCard.classList.contains("hidden") === (stage !== "installing"),
      "安装面板显隐正确");
    // 恢复面板只在已封路阶段显示
    const reopenCard = document.querySelector("#ops-reopen");
    ok(reopenCard.classList.contains("hidden") === (stage !== "closed"),
      "恢复面板显隐正确");
    // 作业结果面板只在封路失败时显示
    const doneCard = document.querySelector("#ops-done");
    ok(doneCard.classList.contains("hidden") === (stage !== "failed"),
      "结果面板仅失败阶段显示");
    // 封路按钮只在安装阶段可点
    ok(document.querySelector("#btn-close").disabled === (stage !== "installing"),
      "封路按钮可用性正确");
    // 安装按钮：待装项在安装阶段可点，其余禁用
    const installArea = document.querySelector("#install-buttons");
    const btns = installArea.children;
    if (stage === "installing") {
      ok(btns.length === 2 && btns[0].disabled && !btns[1].disabled,
        "已装项禁用、待装项可点");
    }
  } catch (e) {
    ok(false, "渲染抛错：" + e.stack);
  }
}

// 核对结果文案
console.log("== 核对文案 ==");
R.state = makeState("installing");
R.renderCheck("#live-box", R.state.live_check);
const liveResult = document.querySelector("#live-box").querySelector(".check-result");
ok(liveResult.children[0].textContent.includes("首个出错路口：B展厅"),
  "现场核对显示首个出错路口");
ok(liveResult.children[0]._cls.has("bad"), "失败判定为红色 bad");
R.renderCheck("#sim-box", R.state.sim_check);
const simResult = document.querySelector("#sim-box").querySelector(".check-result");
ok(simResult.children[0].textContent.includes("所有入口均可沿箭头到达服务台"),
  "预演通过显示成功文案");

// 空状态（无任何数据）
console.log("== 空状态 ==");
R.state = { stage: "setup", stage_error: null, nodes: [], corridors: [], boards: [],
  closure_plan: [], plan_items: [], effective: {},
  live_check: { ok: true, error: null, entrances: [] },
  sim_check: { ok: true, error: null, entrances: [] },
  reopen_check: { ok: true, error: null, entrances: [] } };
try {
  R.renderStage(); R.renderForms(); R.renderLists(); R.renderReopen(); R.renderMap();
  R.renderCheck("#live-box", R.state.live_check); R.renderCheck("#sim-box", R.state.sim_check);
  ok(true, "空数据渲染无异常");
} catch (e) {
  ok(false, "空数据抛错：" + e.stack);
}

process.exit(failures ? 1 : 0);
