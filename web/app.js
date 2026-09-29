/* 换热站联动平台前端（原生 JS，零依赖） */
"use strict";

// ---------- 全局状态 ----------
const state = {
  token: localStorage.getItem("ht_token") || "",
  user: null,
  stationId: null,
  stations: [], buildings: [], points: [],
  tab: "overview",
  curve: { buildingId: null, baseVersion: null, rows: [] },
  cmd: { filter: "", selected: null },
  timer: null,
};

const ROLE_NAMES = { dispatcher: "值班员", manager: "站长", ops: "运维", service: "客服" };
const CMD_STATUS = {
  pending_review: "待复核", pending_execution: "待执行", observing: "生效观察",
  ended: "结束", rejected: "已驳回",
};
const TK_STATE = {
  open: "待处理", processing: "处置中", merged: "已归并", resolved: "已解决",
  reopened: "回访不满意重开",
};
const CAT_NAME = { cold: "不热/冷", hot: "过热", leak: "漏水", other: "其他" };
const EVENT_NAMES = {
  sensor_gap: "传感器断点", temp_diff_abnormal: "温差异常", pressure_drop: "压力骤降",
  pump_switch: "水泵切换", mass_cold: "集中不热", repeat_repair: "重复报修",
  callback_unsatisfied: "回访不满意",
};
const BUILDING_COLORS = ["#0f9d78", "#b07600", "#c94f86", "#4a3aa7",
  "#0f9d78", "#b07600"];
const SUPPLY = "#d8392f", RETBLUE = "#2a78d6";

// ---------- 工具 ----------
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return n;
}
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.token) headers.Authorization = "Bearer " + state.token;
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  const res = await fetch(path, { ...opts, headers });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw Object.assign(new Error(data.error || `请求失败 ${res.status}`),
    { status: res.status });
  return data;
}
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 2600);
}
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtTimeFull(iso) { return iso ? iso.replace("T", " ").slice(0, 16) : "—"; }
function localIso(d) {
  const p = (x) => String(x).padStart(2, "0");
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const oh = Math.floor(Math.abs(off) / 60), om = Math.abs(off) % 60;
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}` +
    `${sign}${p(oh)}:${p(om)}`;
}
function can(...roles) { return state.user && roles.includes(state.user.role); }
function modalShow(node) {
  $("#modal-card").replaceChildren(node);
  $("#modal").classList.remove("hidden");
}
function modalClose() { $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") modalClose(); });

function confirmModal(title, fields, onOk, okText = "确定") {
  const box = el("div", {}, el("h3", {}, title));
  const inputs = {};
  for (const f of fields) {
    box.appendChild(el("label", {}, f.label));
    let inp;
    if (f.type === "select") {
      inp = el("select", { class: "select" },
        ...f.options.map((o) => el("option", { value: o[0] }, o[1])));
    } else if (f.type === "textarea") {
      inp = el("textarea", { class: "input", rows: 3, placeholder: f.ph || "" });
    } else {
      inp = el("input", { class: "input", type: f.type || "text",
        value: f.value || "", placeholder: f.ph || "" });
    }
    inputs[f.key] = inp;
    box.appendChild(inp);
  }
  const err = el("p", { class: "err" });
  box.appendChild(err);
  box.appendChild(el("div", { class: "modal-actions" },
    el("button", { class: "btn", onclick: modalClose }, "取消"),
    el("button", { class: "btn btn-primary", onclick: async () => {
      try { await onOk(inputs); modalClose(); }
      catch (e) { err.textContent = e.message; }
    } }, okText)));
  modalShow(box);
}

// ---------- 登录 ----------
const DEMO_USERS = [
  ["dispatcher1", "王磊", "值班员"], ["dispatcher2", "李芳", "值班员"],
  ["manager1", "赵建国", "站长"], ["ops1", "孙强", "运维"],
  ["service1", "周敏", "客服"],
];
function renderLoginUsers() {
  $("#login-users").replaceChildren(...DEMO_USERS.map(([u, name, role]) =>
    el("button", { onclick: () => doLogin(u) },
      `${name}（${u}）`, el("span", { class: "role-tag" }, role))));
}
async function doLogin(username) {
  try {
    const d = await api("/api/login", { method: "POST", body: { username } });
    state.token = d.token; state.user = d.user;
    localStorage.setItem("ht_token", d.token);
    enterApp();
  } catch (e) { $("#login-err").textContent = e.message; }
}
$("#logout-btn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (e) {}
  state.token = ""; state.user = null;
  localStorage.removeItem("ht_token");
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  clearInterval(state.timer);
});

// ---------- 进入应用 ----------
async function enterApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#user-badge").textContent =
    `${state.user.display_name} · ${ROLE_NAMES[state.user.role]}`;
  const [{ stations }, { buildings }, { points }] = await Promise.all([
    api("/api/stations"), api("/api/buildings"), api("/api/points"),
  ]);
  state.stations = stations; state.buildings = buildings; state.points = points;
  state.stationId = stations[0].id;
  initSelectors();
  switchTab(state.tab);
  clearInterval(state.timer);
  state.timer = setInterval(refreshCurrent, 15000);
  refreshNotifications();
  setInterval(refreshNotifications, 20000);
}

function initSelectors() {
  $("#station-select").replaceChildren(...state.stations.map((s) =>
    el("option", { value: s.id }, s.name)));
  $("#station-select").value = state.stationId;
  const bopts = (bs) => bs.map((b) => el("option", { value: b.id }, b.name));
  $("#curve-building").replaceChildren(...bopts(state.buildings));
  $("#tr-building").replaceChildren(...bopts(state.buildings.filter(
    (b) => b.station_id === +state.stationId)));
  buildOverlayChips();
}
$("#station-select").addEventListener("change", () => {
  state.stationId = +$("#station-select").value;
  initSelectors();
  refreshCurrent();
});
$$(".tab").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
function switchTab(tab) {
  state.tab = tab;
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab-panel").forEach((p) =>
    p.classList.toggle("hidden", p.id !== "tab-" + tab));
  refreshCurrent();
}
function refreshCurrent() {
  ({
    overview: loadOverview, curves: loadCurves, trends: loadTrends,
    commands: loadCommands, tickets: loadTickets, shifts: loadShifts,
    callbacks: loadCallbacks,
  })[state.tab]?.();
}

// ---------- SVG 折线图（少刻度 + crosshair） ----------
function drawLineChart(svgNode, series, opts = {}) {
  const W = +svgNode.viewBox.baseVal.width, H = +svgNode.viewBox.baseVal.height;
  const ml = 46, mr = opts.directLabels ? 96 : 14, mt = 14, mb = 24;
  const iw = W - ml - mr, ih = H - mt - mb;
  svgNode.replaceChildren();
  const NS = "http://www.w3.org/2000/svg";
  const mk = (tag, attrs = {}) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };
  const all = series.flatMap((s) => s.points);
  if (!all.length) {
    svgNode.appendChild(mk("text", { x: W / 2, y: H / 2, "text-anchor": "middle",
      class: "tick-text" }, "暂无数据"));
    return;
  }
  const t0 = Math.min(...all.map((p) => Date.parse(p.ts)));
  const t1 = Math.max(...all.map((p) => Date.parse(p.ts)));
  let y0 = Math.min(...all.map((p) => p.v));
  let y1 = Math.max(...all.map((p) => p.v));
  if (opts.yDomain) [y0, y1] = opts.yDomain;
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.12; y0 -= pad; y1 += pad;
  if (opts.yDomain) [y0, y1] = opts.yDomain;
  const X = (t) => ml + (Date.parse(t) - t0) / Math.max(1, t1 - t0) * iw;
  const Y = (v) => mt + (y1 - v) / (y1 - y0) * ih;

  // 网格 + Y 刻度：仅 4 条
  for (let i = 0; i <= 3; i++) {
    const v = y0 + (y1 - y0) * i / 3, y = Y(v);
    svgNode.appendChild(mk("line", { x1: ml, x2: W - mr, y1: y, y2: y,
      class: "grid-line" }));
    const t = mk("text", { x: ml - 6, y: y + 3.5, "text-anchor": "end",
      class: "tick-text" });
    t.textContent = v.toFixed(opts.yDigits ?? 1);
    svgNode.appendChild(t);
  }
  // X 刻度：仅 5 个时间点
  for (let i = 0; i <= 4; i++) {
    const tt = t0 + (t1 - t0) * i / 4;
    const d = new Date(tt);
    const p = (x) => String(x).padStart(2, "0");
    const t = mk("text", { x: ml + iw * i / 4, y: H - 6, "text-anchor": "middle",
      class: "tick-text" });
    t.textContent = `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    svgNode.appendChild(t);
  }
  // 轴标题
  if (opts.yLabel) {
    const t = mk("text", { x: 12, y: mt + 8, class: "axis-label" });
    t.textContent = opts.yLabel; svgNode.appendChild(t);
  }
  // 折线
  for (const s of series) {
    if (!s.points.length) continue;
    const dPath = s.points.map((p, i) =>
      `${i ? "L" : "M"}${X(p.ts).toFixed(1)},${Y(p.v).toFixed(1)}`).join("");
    svgNode.appendChild(mk("path", { d: dPath,
      class: s.dashed ? "line-old" : "", fill: "none",
      stroke: s.color, "stroke-width": s.width || 2,
      "stroke-dasharray": s.dashed ? "5 4" : null }));
    // 补录点小环
    s.points.filter((p) => p.b).forEach((p) =>
      svgNode.appendChild(mk("circle", { cx: X(p.ts), cy: Y(p.v), r: 3,
        fill: "#fdfefe", stroke: s.color, "stroke-width": 1.6 })));
    // 直接标注：色段 + 中墨文字
    if (opts.directLabels && s.name) {
      const last = s.points[s.points.length - 1];
      const y = Math.min(H - mb - 4, Math.max(mt + 6, Y(last.v)));
      svgNode.appendChild(mk("line", { x1: W - mr + 4, x2: W - mr + 16,
        y1: y, y2: y, stroke: s.color, "stroke-width": 3 }));
      const t = mk("text", { x: W - mr + 20, y: y + 3.5, class: "direct-label",
        fill: "#1b232b" });
      t.textContent = s.name;
      svgNode.appendChild(t);
    }
  }
  // crosshair + tooltip
  const cross = mk("line", { class: "crosshair", visibility: "hidden", y1: mt, y2: mt + ih });
  svgNode.appendChild(cross);
  const tip = mk("g", { visibility: "hidden" });
  svgNode.appendChild(tip);
  const overlay = mk("rect", { x: ml, y: mt, width: iw, height: ih, fill: "transparent" });
  svgNode.appendChild(overlay);
  overlay.addEventListener("mousemove", (e) => {
    const rect = svgNode.getBoundingClientRect();
    const mx = (e.clientX - rect.left) / rect.width * W;
    // 找最近时间（以第一条序列为基准）
    const ref = series.find((s) => s.points.length)?.points || [];
    if (!ref.length) return;
    let best = ref[0], bi = 0;
    for (let i = 0; i < ref.length; i++) {
      if (Math.abs(X(ref[i].ts) - mx) < Math.abs(X(best.ts) - mx)) { best = ref[i]; bi = i; }
    }
    const tx = X(best.ts);
    cross.setAttribute("x1", tx); cross.setAttribute("x2", tx);
    cross.setAttribute("visibility", "visible");
    tip.setAttribute("visibility", "visible");
    tip.replaceChildren();
    const d = new Date(best.ts);
    const p = (x) => String(x).padStart(2, "0");
    const lines = [`${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`];
    for (const s of series) {
      const pp = s.points[bi];
      if (pp) {
        lines.push(`${s.name}: ${pp.v.toFixed(2)}${s.unit || ""}${pp.b ? "（补录）" : ""}`);
        tip.appendChild(mk("circle", { cx: tx, cy: Y(pp.v), r: 4, fill: s.color,
          stroke: "#fdfefe", "stroke-width": 1.5 }));
      }
    }
    const boxW = 150, boxH = 15 + lines.length * 14;
    let bx = tx + 8; if (bx + boxW > W - 4) bx = tx - boxW - 8;
    tip.appendChild(mk("rect", { x: bx, y: mt + 4, width: boxW, height: boxH, rx: 6,
      fill: "#1b232b", opacity: 0.9 }));
    lines.forEach((ln, i) => {
      const t = mk("text", { x: bx + 8, y: mt + 18 + i * 14, fill: "#fff",
        "font-size": i ? 11 : 11.5, "font-weight": i ? 400 : 600 });
      t.textContent = ln; tip.appendChild(t);
    });
  });
  overlay.addEventListener("mouseleave", () => {
    cross.setAttribute("visibility", "hidden");
    tip.setAttribute("visibility", "hidden");
  });
}

// 曲线坐标图（X=室外温度），用于编辑预览与审批对照
function drawCurveChart(svgNode, curvesList, opts = {}) {
  const W = +svgNode.viewBox.baseVal.width, H = +svgNode.viewBox.baseVal.height;
  const ml = 40, mr = 12, mt = 12, mb = 28, iw = W - ml - mr, ih = H - mt - mb;
  svgNode.replaceChildren();
  const NS = "http://www.w3.org/2000/svg";
  const mk = (tag, a = {}) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(a)) if (v != null) n.setAttribute(k, v);
    return n;
  };
  const all = curvesList.flatMap((c) => c.points);
  if (!all.length) return;
  let x0 = Math.min(...all.map((p) => p.outdoor)), x1 = Math.max(...all.map((p) => p.outdoor));
  if (x0 === x1) { x0 -= 1; x1 += 1; }
  const [y0, y1] = [30, 85];
  const X = (o) => ml + (o - x0) / (x1 - x0) * iw;
  const Y = (v) => mt + (y1 - v) / (y1 - y0) * ih;
  for (let i = 0; i <= 3; i++) {
    const v = y0 + (y1 - y0) * i / 3, y = Y(v);
    svgNode.appendChild(mk("line", { x1: ml, x2: W - mr, y1: y, y2: y, class: "grid-line" }));
    const t = mk("text", { x: ml - 5, y: y + 3.5, "text-anchor": "end", class: "tick-text" });
    t.textContent = v.toFixed(0); svgNode.appendChild(t);
  }
  for (let i = 0; i <= 4; i++) {
    const o = x0 + (x1 - x0) * i / 4;
    const t = mk("text", { x: X(o), y: H - 8, "text-anchor": "middle", class: "tick-text" });
    t.textContent = o.toFixed(0); svgNode.appendChild(t);
  }
  const xt = mk("text", { x: W / 2, y: H - 0, "text-anchor": "middle", class: "axis-label" });
  xt.textContent = "室外温度 ℃"; svgNode.appendChild(xt);
  for (const c of curvesList) {
    for (const key of ["supply", "return"]) {
      const dPath = c.points.map((p, i) =>
        `${i ? "L" : "M"}${X(p.outdoor).toFixed(1)},${Y(p[key]).toFixed(1)}`).join("");
      svgNode.appendChild(mk("path", { d: dPath, fill: "none",
        stroke: key === "supply" ? SUPPLY : RETBLUE, "stroke-width": c.dashed ? 1.5 : 2,
        "stroke-dasharray": c.dashed ? "5 4" : null, opacity: c.faded ? 0.55 : 1 }));
    }
  }
}

// 分组柱状图（回访统计）
function drawBars(svgNode, rows) {
  const W = 480, H = 260, ml = 40, mr = 12, mt = 16, mb = 40;
  const iw = W - ml - mr, ih = H - mt - mb;
  svgNode.replaceChildren();
  const NS = "http://www.w3.org/2000/svg";
  const mk = (tag, a = {}) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(a)) n.setAttribute(k, v);
    return n;
  };
  const maxV = Math.max(1, ...rows.map((r) => r.sat + r.dis));
  const gw = iw / rows.length;
  for (let i = 0; i <= 3; i++) {
    const v = maxV * i / 3, y = mt + ih - v / maxV * ih;
    svgNode.appendChild(mk("line", { x1: ml, x2: W - mr, y1: y, y2: y, class: "grid-line" }));
    const t = mk("text", { x: ml - 6, y: y + 3.5, "text-anchor": "end", class: "tick-text" });
    t.textContent = Math.round(v); svgNode.appendChild(t);
  }
  rows.forEach((r, i) => {
    const cx = ml + gw * i + gw / 2, bw = Math.min(34, gw * 0.5);
    const hs = r.sat / maxV * ih, hd = r.dis / maxV * ih;
    svgNode.appendChild(mk("rect", { x: cx - bw - 2, y: mt + ih - hs, width: bw,
      height: Math.max(0, hs), rx: 4, class: "bar-sat" }));
    svgNode.appendChild(mk("rect", { x: cx + 2, y: mt + ih - hd, width: bw,
      height: Math.max(0, hd), rx: 4, class: "bar-dis" }));
    if (r.sat) {
      const t = mk("text", { x: cx - bw / 2 - 2, y: mt + ih - hs - 4, "text-anchor": "middle",
        class: "tick-text" }); t.textContent = r.sat; svgNode.appendChild(t);
    }
    if (r.dis) {
      const t = mk("text", { x: cx + bw / 2 + 2, y: mt + ih - hd - 4, "text-anchor": "middle",
        class: "tick-text" }); t.textContent = r.dis; svgNode.appendChild(t);
    }
    const t = mk("text", { x: cx, y: H - 14, "text-anchor": "middle", class: "axis-label" });
    t.textContent = CAT_NAME[r.category] || r.category; svgNode.appendChild(t);
  });
}

// ================= 站点总览 =================
async function loadOverview() {
  const sid = state.stationId;
  const d = await api(`/api/overview?station_id=${sid}`);
  const stationPts = d.readings.filter((r) => !r.building_id);
  const od = stationPts.find((p) => p.kind === "outdoor_temp");
  const ps = stationPts.find((p) => p.kind === "supply_pressure");
  const pr = stationPts.find((p) => p.kind === "return_pressure");
  const openEvents = Object.values(d.event_counts).reduce((a, b) => a + b, 0);
  const kpi = (k, v, cls = "") => el("div", { class: "kpi " + cls },
    el("div", { class: "k" }, k), el("div", { class: "v" }, v));
  $("#ov-kpis").replaceChildren(
    kpi("当前室外温度", od?.latest_value != null ? `${od.latest_value.toFixed(1)}℃` : "—"),
    kpi("供水压力 / 温度参考", ps?.latest_value != null
      ? `${ps.latest_value.toFixed(2)} MPa` : "—", "supply"),
    kpi("回水压力", pr?.latest_value != null ? `${pr.latest_value.toFixed(2)} MPa` : "—",
      "returnc"),
    kpi("待处置事件", openEvents, openEvents ? "alert" : ""),
    kpi("待处理报修", d.open_tickets, d.open_tickets ? "alert" : ""),
    kpi("当前班次", d.active_shift
      ? `#${d.active_shift.seq} ${d.active_shift.operator_name}` : "未开班"),
  );
  $("#ov-readings").replaceChildren(...d.readings.map((r) => {
    const ageMin = r.latest_ts ? (Date.now() - Date.parse(r.latest_ts)) / 60000 : 9999;
    const gap = ageMin > 4;
    const cls = r.kind.startsWith("supply") ? "s" : r.kind.startsWith("return") ? "r" : "";
    return el("div", { class: "reading" },
      el("div", { class: "lab" }, r.label),
      el("div", { class: "val " + cls },
        r.latest_value != null ? `${r.latest_value.toFixed(r.kind.endsWith("pressure") ? 2 : 1)}`
          : "无数据",
        el("span", { class: "tag-backfill" }, r.backfilled ? "补录" : null),
        el("span", { class: "tag-gap" }, gap && r.latest_value != null ? "断点" : null)),
      el("div", { class: "ts" }, r.latest_ts ? fmtTime(r.latest_ts) : "尚无读数"));
  }));
  const evs = await api("/api/events?status=open");
  if (!evs.events.length) {
    $("#ov-events").replaceChildren(el("p", { class: "muted" }, "暂无待处置事件"));
  } else {
    // 按七类分组展示
    const groups = {};
    evs.events.forEach((e) => (groups[e.type] ||= []).push(e));
    $("#ov-events").replaceChildren(...Object.entries(groups).map(([type, list]) =>
      el("div", { class: "event-item" },
        el("div", {},
          el("span", { class: `pill ${list[0].severity}` }, EVENT_NAMES[type]),
          " ×", String(list.length),
          el("span", { class: "meta" },
            `　最近：${fmtTime(list[0].detected_at)}　${list[0].building_name || list[0].station_name || ""}`)),
        el("div", { class: "event-actions" },
          el("button", { class: "btn btn-ghost", onclick: () => {
            state.tab = "tickets"; switchTab("tickets");
          } }, "前往处置")))));
  }
  const cc = d.command_counts;
  const box = (label, n) => el("div", { class: "box" },
    el("div", { class: "muted" }, label), el("b", {}, String(n || 0)));
  $("#ov-commands").replaceChildren(
    box("待复核", cc.pending_review), box("待执行", cc.pending_execution),
    box("生效观察", cc.observing), box("本月已结束", cc.ended),
    box("已驳回", cc.rejected));
}

// ================= 曲线编辑 =================
async function loadCurves() {
  const bid = +$("#curve-building").value;
  if (!bid) return;
  state.curve.buildingId = bid;
  const { versions } = await api(`/api/buildings/${bid}/curves`);
  const head = versions[0];
  // 仅在切换楼栋时锁定基准版本；之后他人提交新版本不自动覆盖，提交时产生冲突提示
  if (state.curve._loadedFor !== bid) {
    state.curve.baseVersion = head.version_no;
    state.curve.rows = JSON.parse(JSON.stringify(
      versions.find((v) => v.status === "active")?.points || head.points));
    state.curve._loadedFor = bid;
    $("#curve-note").value = "";
  }
  $("#curve-base").value = "v" + state.curve.baseVersion
    + (head.version_no > state.curve.baseVersion
      ? `（最新 v${head.version_no}）` : "");
  renderCurveTable();
  drawCurvePreview(versions);
  $("#curve-history").replaceChildren(...versions.map((v) =>
    el("div", {},
      `v${v.version_no} · ${v.status === "active" ? "✅生效中" :
        { proposed: "待审批", superseded: "已替代", rejected: "已驳回" }[v.status] || v.status}`,
      el("span", { class: "meta" },
        `　${fmtTime(v.created_at)} ${v.creator_name}｜${v.note || ""}`))));
  const msg = $("#curve-msg");
  if (head.version_no > state.curve.baseVersion) {
    msg.className = "msg err";
    msg.replaceChildren(
      `⚠ 版本已变化：您基于 v${state.curve.baseVersion} 编辑，他人已提交 v${head.version_no}，` +
      "现在提交将收到冲突提示。",
      el("button", { class: "btn btn-ghost", style: "margin-left:10px", onclick: () => {
        state.curve.baseVersion = head.version_no;
        state.curve.rows = JSON.parse(JSON.stringify(
          versions.find((v) => v.status === "active")?.points || head.points));
        loadCurves();
        toast("已基于最新版本重新载入");
      } }, "基于最新版本重新编辑"));
  }
}
function renderCurveTable() {
  const tb = $("#curve-table tbody");
  tb.replaceChildren(...state.curve.rows.map((p, i) =>
    el("tr", {},
      el("td", {}, numInput(p, "outdoor", i)),
      el("td", {}, numInput(p, "supply", i)),
      el("td", {}, numInput(p, "return", i)),
      el("td", {}, el("button", { class: "btn btn-ghost",
        onclick: () => { state.curve.rows.splice(i, 1); renderCurveTable(); drawCurvePreview(); } },
        "删除")))));
}
function numInput(p, key, i) {
  return el("input", { class: "input", type: "number", step: "0.5", value: p[key],
    oninput: (e) => {
      p[key] = +e.target.value; drawCurvePreview();
    } });
}
$("#curve-add").addEventListener("click", () => {
  const last = state.curve.rows[state.curve.rows.length - 1] ||
    { outdoor: 0, supply: 48, return: 40 };
  state.curve.rows.push({ outdoor: last.outdoor + 2, supply: last.supply - 2,
    return: last["return"] - 2 });
  renderCurveTable(); drawCurvePreview();
});
$("#curve-building").addEventListener("change", () => {
  state.curve.rows = []; state.curve.baseVersion = null; state.curve._loadedFor = null;
  $("#curve-msg").textContent = "";
  loadCurves();
});
function drawCurvePreview(versions) {
  const rows = [...state.curve.rows].sort((a, b) => a.outdoor - b.outdoor);
  const active = versions.find((v) => v.status === "active");
  drawCurveChart($("#curve-svg"), [
    { points: rows },
    ...(active ? [{ points: active.points, dashed: true, faded: true }] : []),
  ]);
  // 前端轻校验提示（不阻断提交，后端为最终判定）
  const msg = $("#curve-msg");
  const warns = [];
  rows.forEach((p) => {
    if (p.supply > 75 || p.supply < 40) warns.push(`供水 ${p.supply}℃ 越限`);
    const d = p.supply - p.return;
    if (d > 20 || d < 5) warns.push(`温差 ${d.toFixed(1)}℃ 越限`);
  });
  if (warns.length && msg.className.indexOf("err") === -1) {
    msg.className = "msg warn";
    msg.textContent = "可能越限（需站长批准）：" + [...new Set(warns)].join("；");
  } else if (!warns.length && msg.className.indexOf("err") === -1) {
    msg.className = "msg";
    msg.textContent = "";
  }
}
$("#curve-submit").addEventListener("click", async () => {
  try {
    const d = await api(
      `/api/buildings/${state.curve.buildingId}/commands`, {
        method: "POST",
        body: { points: state.curve.rows, base_version: state.curve.baseVersion,
          note: $("#curve-note").value },
      });
    if (d.breach) {
      toast(`指令 #${d.id} 已提交：越限需站长批准（${d.breach_reason}）`, "err");
    } else {
      toast(`指令 #${d.id} 已提交，进入待复核`, "ok");
    }
    state.curve.rows = []; state.curve._loadedFor = null;
    $("#curve-note").value = "";
    loadCurves();
  } catch (e) {
    const msg = $("#curve-msg");
    msg.className = "msg err";
    if (e.status === 409) {
      msg.textContent = "⚠ 版本冲突：" + e.message;
      state.curve.baseVersion = null;
      loadCurves();
    } else msg.textContent = e.message;
  }
});

// ================= 实时趋势 =================
function stationBuildings() {
  return state.buildings.filter((b) => b.station_id === +state.stationId);
}
function buildOverlayChips() {
  const box = $("#tr-overlay-pick");
  const bs = stationBuildings();
  box.replaceChildren(...bs.map((b, i) =>
    el("span", { class: "chip" + (i < 3 ? " on" : ""),
      style: i < 3 ? `background:${BUILDING_COLORS[i]}` : "",
      "data-bid": b.id, onclick: (e) => {
        const n = box.querySelectorAll(".chip.on").length;
        const on = e.target.classList.contains("on");
        if (on && n <= 1) return toast("至少保留一个楼栋");
        e.target.classList.toggle("on");
        const idx = $$("#tr-overlay-pick .chip").indexOf(e.target);
        e.target.style.background = !on ? "" : BUILDING_COLORS[idx];
        loadTrends();
      } }, b.name)));
}
$("#tr-range").addEventListener("change", loadTrends);
$("#tr-mode").addEventListener("change", () => {
  const overlay = $("#tr-mode").value === "overlay";
  $("#tr-building").classList.toggle("hidden", overlay);
  $("#tr-overlay-pick").classList.toggle("hidden", !overlay);
  loadTrends();
});
$("#tr-building").addEventListener("change", loadTrends);
async function loadTrends() {
  const hours = +$("#tr-range").value;
  const start = localIso(new Date(Date.now() - hours * 3600 * 1000)).slice(0, 16);
  const sp = state.points;
  const station = +state.stationId;
  const od = sp.find((p) => p.station_id === station && p.kind === "outdoor_temp");
  const ps = sp.find((p) => p.station_id === station && p.kind === "supply_pressure");
  const pr = sp.find((p) => p.station_id === station && p.kind === "return_pressure");
  const { series: data } = await api(
    `/api/series?start=${encodeURIComponent(start)}&points=` +
    [od?.id, ps?.id, pr?.id].filter(Boolean).join(","));
  drawLineChart($("#tr-od"), [{ name: "室外温度", color: "#56606b",
    points: (data[od.id] || []), unit: "℃" }], { yLabel: "℃" });
  drawLineChart($("#tr-pressure"), [
    { name: "供水压力", color: SUPPLY, points: data[ps.id] || [], unit: "MPa" },
    { name: "回水压力", color: RETBLUE, points: data[pr.id] || [], unit: "MPa" },
  ], { yLabel: "MPa", yDigits: 2 });

  if ($("#tr-mode").value === "single") {
    $("#tr-legend").replaceChildren(
      el("span", {}, el("i", { class: "lg lg-supply" }), "供水温度"),
      el("span", {}, el("i", { class: "lg lg-return" }), "回水温度"));
    const bid = +$("#tr-building").value;
    const sup = sp.find((p) => p.building_id === bid && p.kind === "supply_temp");
    const ret = sp.find((p) => p.building_id === bid && p.kind === "return_temp");
    const d2 = await api(`/api/series?start=${encodeURIComponent(start)}&points=${sup.id},${ret.id}`);
    drawLineChart($("#tr-temp"), [
      { name: "供水温度", color: SUPPLY, points: d2.series[sup.id] || [], unit: "℃" },
      { name: "回水温度", color: RETBLUE, points: d2.series[ret.id] || [], unit: "℃" },
    ], { yLabel: "℃" });
  } else {
    const chips = $$("#tr-overlay-pick .chip.on");
    const bids = chips.map((c) => +c.dataset.bid);
    const sups = bids.map((bid) => sp.find((p) => p.building_id === bid
      && p.kind === "supply_temp"));
    const d2 = await api(`/api/series?start=${encodeURIComponent(start)}&points=`
      + sups.map((p) => p.id).join(","));
    const nameOf = (pid) => state.buildings.find((b) => b.id === sups.find(
      (p) => p.id === pid)?.building_id)?.name;
    const allChips = $$("#tr-overlay-pick .chip");
    const chartSeries = sups.map((p, i) => ({
      name: nameOf(p.id), color: BUILDING_COLORS[allChips.indexOf(chips[i])],
      points: d2.series[p.id] || [], unit: "℃",
    }));
    $("#tr-legend").replaceChildren(...chartSeries.map((s) =>
      el("span", {}, el("i", { class: "lg", style: `background:${s.color}` }), s.name)));
    drawLineChart($("#tr-temp"), chartSeries, { yLabel: "供水℃", directLabels: true });
  }
}

// ================= 指令审批 =================
$("#cmd-filter").addEventListener("change", () => { state.cmd.filter = $("#cmd-filter").value; loadCommands(); });
async function loadCommands() {
  const d = await api("/api/commands?status=" + state.cmd.filter);
  const list = $("#cmd-list");
  list.replaceChildren(...d.commands.map((c) =>
    el("div", { class: "cmd-item" + (state.cmd.selected === c.id ? " sel" : ""),
      onclick: () => { state.cmd.selected = c.id; loadCommands(); } },
      el("div", {},
        el("strong", {}, `#${c.id} ${c.building_name}`),
        c.breach ? el("span", { class: "pill breach" }, "越限") : null,
        " ",
        el("span", { class: "pill" }, CMD_STATUS[c.status])),
      el("div", { class: "meta" },
        `${fmtTime(c.submitted_at)} 提交 · ${c.submitter_name} · 关联报修 ${c.ticket_count}`))));
  if (state.cmd.selected) renderCommandDetail(state.cmd.selected);
}
async function renderCommandDetail(cid) {
  const c = await api("/api/commands/" + cid);
  const oldPane = el("div", { class: "pane" },
    el("h4", {}, "旧曲线（当前生效）"),
    c.old_points.length
      ? el("div", {}, curveMiniSvg(c.old_points), anchorTable(c.old_points))
      : el("p", { class: "muted" }, "无（首次设定）"));
  const newPane = el("div", { class: "pane new" },
    el("h4", {}, "新曲线（待生效）",
      c.breach ? el("span", { class: "pill breach" }, "越限，需站长批准") : null),
    el("div", {}, curveMiniSvg(c.new_points), anchorTable(c.new_points, c.old_points)));
  const compare = el("div", { class: "compare" }, oldPane, newPane);

  const box = el("div", {},
    el("div", { class: "kv" },
      el("span", { class: "k" }, "楼栋"), el("span", {}, c.building_name),
      el("span", { class: "k" }, "状态"), el("span", {}, CMD_STATUS[c.status]),
      el("span", { class: "k" }, "提交人"), el("span", {}, c.submitter_name),
      el("span", { class: "k" }, "提交时间"), el("span", {}, fmtTimeFull(c.submitted_at)),
      el("span", { class: "k" }, "越限原因"),
      el("span", { class: c.breach ? "err" : "muted" }, c.breach_reason || "无")),
    compare,
    c.reviewed_at ? el("p", { class: "muted" },
      `复核：${c.reviewer_name || ""} ${fmtTimeFull(c.reviewed_at)} ${c.review_note}`) : null,
    c.executed_at ? el("p", { class: "muted" },
      `执行：${c.executor_name || ""} ${fmtTimeFull(c.executed_at)}`) : null);

  // 观察期关联报修
  if (["observing", "ended"].includes(c.status)) {
    box.appendChild(el("h4", {}, `生效观察（${c.observe_minutes} 分钟）· 同楼栋冷暖报修`));
    if (!c.tickets.length) box.appendChild(el("p", { class: "linked-tk" }, "观察窗内暂无关联报修"));
    else box.appendChild(el("div", {}, ...c.tickets.map((t) =>
      el("div", { class: "linked-tk" },
        `#${t.id} ${t.room || ""} ${CAT_NAME[t.category]} · ${TK_STATE[t.state]} · ${fmtTime(t.created_at)}`))));
    box.appendChild(el("p", { class: "muted" }, c.effect || "观察窗内持续自动关联新报修"));
  }

  // 操作区（按角色与状态）
  const actions = el("div", { class: "review-box" });
  const noteInput = () => el("textarea", { class: "input", id: "cmd-note",
    placeholder: "复核意见 / 执行备注 / 结束说明" });
  const ni = noteInput();
  if (c.status === "pending_review") {
    const canApprove = can("manager") || (!c.breach && can("dispatcher"));
    actions.appendChild(ni);
    actions.appendChild(el("div", {},
      el("button", {
        class: "btn btn-primary", disabled: canApprove ? null : "disabled",
        title: canApprove ? "" : (c.breach ? "越限指令必须站长批准" : "无权复核"),
        onclick: async () => {
          try { await api(`/api/commands/${cid}/review`, { method: "POST",
            body: { approve: true, note: $("#cmd-note").value } });
            toast("已复核通过，进入待执行", "ok"); loadCommands();
          } catch (e) { toast(e.message, "err"); }
        } }, "复核通过（转待执行）"),
      el("button", { class: "btn btn-danger", onclick: async () => {
        await api(`/api/commands/${cid}/review`, { method: "POST",
          body: { approve: false, note: $("#cmd-note").value } });
        toast("已驳回"); loadCommands();
      } }, "驳回")));
    if (!canApprove && c.breach)
      actions.appendChild(el("p", { class: "err" }, "越限指令必须由站长批准；运维仅有执行权限。"));
  } else if (c.status === "pending_execution") {
    actions.appendChild(ni);
    actions.appendChild(el("button", {
      class: "btn btn-primary", disabled: can("ops", "manager") ? null : "disabled",
      title: can("ops", "manager") ? "" : "仅运维人员可执行指令",
      onclick: async () => {
        try { await api(`/api/commands/${cid}/execute`, { method: "POST" });
          toast("已执行，进入生效观察期", "ok"); loadCommands();
        } catch (e) { toast(e.message, "err"); }
      } }, "执行指令（运维）"));
    if (!can("ops", "manager"))
      actions.appendChild(el("p", { class: "err" }, "仅运维/站长可执行；提交人不能自行执行审批链路。"));
  } else if (c.status === "observing") {
    actions.appendChild(ni);
    actions.appendChild(el("div", {},
      el("button", { class: "btn btn-primary", onclick: () => endCmd(cid, "effective") }, "判定有效并结束"),
      el("button", { class: "btn", onclick: () => endCmd(cid, "ineffective") }, "判定无效"),
      el("button", { class: "btn btn-ghost", onclick: () => endCmd(cid, "uncertain") }, "无法判定")));
  } else {
    actions.appendChild(el("p", { class: "muted" },
      c.status === "ended" ? `已结束：${{ effective: "调节有效", ineffective: "调节无效",
        uncertain: "效果不确定" }[c.effect_judgement] || ""}` : "该指令已驳回"));
  }
  box.appendChild(actions);
  $("#cmd-detail").replaceChildren(box);
}
async function endCmd(cid, judgement) {
  try {
    await api(`/api/commands/${cid}/end`, { method: "POST",
      body: { judgement, note: $("#cmd-note")?.value || "" } });
    toast("观察结束，指令归档", "ok"); loadCommands();
  } catch (e) { toast(e.message, "err"); }
}
function curveMiniSvg(points) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 460 190");
  drawCurveChart(svg, [{ points }]);
  return svg;
}
function anchorTable(points, oldPoints) {
  const oldAt = (o) => {
    if (!oldPoints) return null;
    for (let i = 0; i < oldPoints.length; i++) {
      if (oldPoints[i].outdoor === o) return oldPoints[i];
      if (i < oldPoints.length - 1 && oldPoints[i].outdoor < o && o < oldPoints[i + 1].outdoor) {
        const t = (o - oldPoints[i].outdoor) / (oldPoints[i + 1].outdoor - oldPoints[i].outdoor);
        return { supply: oldPoints[i].supply + t * (oldPoints[i + 1].supply - oldPoints[i].supply),
          return: oldPoints[i].return + t * (oldPoints[i + 1].return - oldPoints[i].return) };
      }
    }
    return null;
  };
  return el("table", { class: "table" },
    el("thead", {}, el("tr", {},
      el("th", {}, "室外℃"), el("th", {}, "供水℃"), el("th", {}, "回水℃"),
      el("th", {}, "供水变化"))),
    el("tbody", {}, ...points.map((p) => {
      const o = oldAt(p.outdoor);
      const d = o ? p.supply - o.supply : null;
      return el("tr", {},
        el("td", {}, String(p.outdoor)),
        el("td", {}, String(p.supply)),
        el("td", {}, String(p["return"])),
        el("td", {}, d == null ? el("span", { class: "muted" }, "—")
          : el("span", { style: `color:${d > 0 ? SUPPLY : RETBLUE}` },
            `${d > 0 ? "+" : ""}${d.toFixed(1)}`)));
    })));
}

// ================= 报修聚合 =================
$("#tk-state").addEventListener("change", loadTickets);
$("#ev-status").addEventListener("change", loadTickets);
$("#tk-new").addEventListener("click", () => {
  if (!can("service", "dispatcher", "manager"))
    return toast("仅客服/值班员可登记报修", "err");
  const bs = state.buildings;
  confirmModal("登记居民报修", [
    { key: "building_id", label: "楼栋", type: "select",
      options: bs.map((b) => [b.id, b.name]) },
    { key: "room", label: "房号（如 1单元301）", ph: "1单元301" },
    { key: "category", label: "类别", type: "select",
      options: [["cold", "不热/冷"], ["hot", "过热"], ["leak", "漏水"], ["other", "其他"]] },
    { key: "channel", label: "来源", type: "select",
      options: [["电话", "电话"], ["小程序", "小程序"], ["上门", "上门"]] },
    { key: "description", label: "问题描述", type: "textarea", ph: "住户描述" },
  ], async (i) => {
    const d = await api("/api/tickets", { method: "POST", body: {
      building_id: +i.building_id.value, room: i.room.value, category: i.category.value,
      channel: i.channel.value, description: i.description.value } });
    toast(`报修单 #${d.id} 已登记${d.state === "merged" ? "，自动归并为重复报修" : ""}`, "ok");
    loadTickets();
  }, "提交");
});
async function loadTickets() {
  const [{ rows }, { tickets }, { events }] = await Promise.all([
    api("/api/tickets/density?hours=24"),
    api("/api/tickets?state=" + $("#tk-state").value),
    api("/api/events?status=" + $("#ev-status").value + "&type="),
  ]);
  const max = Math.max(1, ...rows.map((r) => r.total_n));
  $("#tk-density").replaceChildren(...rows.map((r) => {
    const seg = (cls, n) => n
      ? el("i", { class: cls, style: `width:${n / r.total_n * 100}%` }) : null;
    return el("tr", {},
      el("td", {}, r.station_name), el("td", {}, r.building_name),
      el("td", { class: "num" }, String(r.cold_n)),
      el("td", { class: "num" }, String(r.hot_n)),
      el("td", { class: "num" }, String(r.other_n)),
      el("td", { class: "num" }, String(r.open_n)),
      el("td", {}, el("div", { class: "density-bar",
        style: `width:${30 + r.ratio * 130}px` },
        seg("c-cold", r.cold_n), seg("c-hot", r.hot_n), seg("c-other", r.other_n))));
  }));

  $("#tk-list").replaceChildren(...tickets.map((t) =>
    el("div", { class: "ticket-item" },
      el("div", {},
        el("strong", {}, `#${t.id} ${t.building_name} ${t.room || ""}`), " ",
        el("span", { class: "pill" }, CAT_NAME[t.category]), " ",
        el("span", { class: "pill" }, TK_STATE[t.state]),
        t.master_id ? el("span", { class: "pill" }, `归并至 #${t.master_id}`) : null),
      el("div", { class: "meta" },
        `${fmtTime(t.created_at)} · ${t.channel} · 联系人：${t.resident_name || "匿名"} ${t.resident_phone || ""} · ${t.description}`),
      t.handle_note ? el("div", { class: "meta" }, "处置：" + t.handle_note) : null,
      t.callback_state !== "none" ? el("div", { class: "meta" },
        `回访：${t.callback_state === "satisfied" ? "满意" : "不满意（已重开）"} ${t.callback_note || ""}`) : null,
      ticketActions(t))));

  $("#ev-list").replaceChildren(...events.map((e) => {
    let detail = "";
    try {
      const p = JSON.parse(e.payload_json || "{}");
      detail = p.diff != null ? `供回水温差 ${p.diff}℃`
        : p.drop != null ? `5 分钟压降 ${p.drop} MPa（${p.from}→${p.to}）`
        : p.to_pump != null ? `${p.from_pump}# 泵切换至 ${p.to_pump}# 泵`
        : p.count != null ? `${p.window_min} 分钟内 ${p.count} 件冷暖报修`
        : p.age_min != null ? `已中断约 ${p.age_min} 分钟（最后读数 ${p.last_ts ? fmtTime(p.last_ts) : "—"}）`
        : p.note || "";
    } catch (err) {}
    return el("div", { class: "event-item" },
      el("div", {},
        el("span", { class: `pill ${e.severity} st-${e.type}` }, EVENT_NAMES[e.type]), " ",
        el("span", { class: "meta" },
          `#${e.id} ${e.building_name ? e.building_name + " " : ""}${fmtTime(e.detected_at)}　${detail}`)),
      e.status === "handled"
        ? el("div", { class: "meta" }, `已处置：${e.handler_name}｜${e.resolution}`)
        : eventActions(e));
  }));
}
function ticketActions(t) {
  const wrap = el("div", { class: "ticket-actions" });
  if (["open", "reopened"].includes(t.state) && can("service", "ops", "dispatcher", "manager")) {
    wrap.appendChild(el("button", { class: "btn btn-ghost", onclick: () =>
      confirmModal(`报修 #${t.id} 登记处置`, [{ key: "note", label: "处置措施", type: "textarea" }],
        async (i) => { await api(`/api/tickets/${t.id}/handle`, { method: "POST",
          body: { note: i.note.value } }); toast("已登记处置", "ok"); loadTickets(); }, "保存") },
      "登记处置"));
  }
  if (["open", "processing", "reopened"].includes(t.state) && can("ops", "dispatcher", "manager")) {
    wrap.appendChild(el("button", { class: "btn btn-ghost", onclick: () =>
      confirmModal(`报修 #${t.id} 解决反馈`, [{ key: "note", label: "解决说明", type: "textarea" }],
        async (i) => { await api(`/api/tickets/${t.id}/resolve`, { method: "POST",
          body: { note: i.note.value } }); toast("已标记解决，待回访", "ok"); loadTickets(); }, "确认解决") },
      "标记解决"));
  }
  if (t.state === "resolved" || (t.state === "processing" && t.callback_state === "none")) {
    if (can("service")) {
      wrap.appendChild(el("button", { class: "btn btn-primary", onclick: () => callbackTicket(t, true) }, "回访满意"));
      wrap.appendChild(el("button", { class: "btn btn-danger", onclick: () => callbackTicket(t, false) }, "回访不满意"));
    } else {
      wrap.appendChild(el("span", { class: "meta" }, "（客服回访）"));
    }
  }
  return wrap;
}
async function callbackTicket(t, satisfied) {
  confirmModal(satisfied ? "回访满意" : "回访不满意（工单将重开）",
    [{ key: "note", label: "回访记录", type: "textarea" }],
    async (i) => {
      await api(`/api/tickets/${t.id}/callback`, { method: "POST",
        body: { satisfied, note: i.note.value } });
      toast(satisfied ? "回访完成" : "不满意，工单已重开并生成事件", satisfied ? "ok" : "err");
      loadTickets();
    }, "提交回访");
}
function eventActions(e) {
  const wrap = el("div", { class: "event-actions" });
  const resolve = async (text) => {
    await api(`/api/events/${e.id}/handle`, { method: "POST", body: { resolution: text } });
    toast("事件已处置", "ok"); loadTickets();
  };
  if (e.type === "sensor_gap" && can("ops", "manager")) {
    wrap.appendChild(el("button", { class: "btn btn-primary", onclick: () => {
      const p = state.points.find((x) => x.id === JSON.parse(e.payload_json).point_id
        || x.code === JSON.parse(e.payload_json).point_code);
      confirmModal(`断点补录：${p?.label || "测点"}`, [
        { key: "ts", label: "补录时刻（本地时间，格式 2026-09-29T10:00）",
          value: localIso(new Date()).slice(0, 16), ph: "YYYY-MM-DDTHH:MM" },
        { key: "value", label: "补录数值", type: "number" },
      ], async (i) => {
        await api(`/api/points/${p.id}/backfill`, { method: "POST",
          body: { ts: i.ts.value.length === 16 ? i.ts.value + ":00" : i.ts.value,
            value: +i.value.value } });
        toast("断点补录成功，事件自动关闭", "ok"); loadTickets(); loadOverview();
      }, "补录");
    } }, "断点补录"));
  } else if (e.type === "pump_switch" && can("ops", "manager")) {
    wrap.appendChild(el("button", { class: "btn btn-primary", onclick: () =>
      confirmModal("确认水泵切换", [{ key: "r", label: "处置记录", type: "textarea" }],
        async (i) => resolve(i.r.value), "确认") }, "记录切换"));
  } else if (["temp_diff_abnormal", "pressure_drop", "mass_cold"].includes(e.type)
      && can("dispatcher", "manager")) {
    wrap.appendChild(el("button", { class: "btn btn-primary", onclick: () =>
      confirmModal(EVENT_NAMES[e.type] + "处置",
        [{ key: "r", label: "调度处置措施", type: "textarea" }],
        async (i) => resolve(i.r.value), "完成处置") }, "完成处置"));
    if (e.type === "mass_cold")
      wrap.appendChild(el("button", { class: "btn", onclick: () => switchTab("curves") }, "去调曲线"));
  } else if (e.type === "repeat_repair" && can("service")) {
    wrap.appendChild(el("button", { class: "btn btn-primary", onclick: () => {
      const p = JSON.parse(e.payload_json);
      confirmModal(EVENT_NAMES[e.type] + "处置",
        [{ key: "r", label: "客服处置（联系/重新派单）", type: "textarea" }],
        async (i) => resolve(i.r.value), "完成");
    } }, "联系住户并处置"));
  } else if (e.type === "callback_unsatisfied" && can("service", "dispatcher")) {
    wrap.appendChild(el("button", { class: "btn btn-primary", onclick: () => {
      const p = JSON.parse(e.payload_json);
      if (p.ticket_id) { state.tab = "tickets"; $("#tk-state").value = ""; loadTickets(); }
      confirmModal(EVENT_NAMES[e.type] + "处置",
        [{ key: "r", label: "客服处置（联系/重新派单）", type: "textarea" }],
        async (i) => resolve(i.r.value), "完成");
    } }, "查看并重派"));
  } else {
    wrap.appendChild(el("span", { class: "meta" }, "需对应岗位处置（见角色权限）"));
  }
  return wrap;
}

// ================= 班次交接 =================
async function loadShifts() {
  const sid = state.stationId;
  const [{ shifts }, ov] = await Promise.all([
    api(`/api/stations/${sid}/shifts`), api(`/api/overview?station_id=${sid}`)]);
  const a = ov.active_shift;
  const active = $("#sh-active");
  if (a) {
    active.replaceChildren(
      el("p", {}, el("strong", {}, `#${a.seq} ${a.kind === "day" ? "白班" : "夜班"}`),
        `　值班员：${a.operator_name}　开始：${fmtTimeFull(a.started_at)}`),
      el("div", { class: "event-actions" },
        el("button", { class: "btn", onclick: () => confirmModal("形成运行快照", [
          { key: "note", label: "快照说明", ph: "如：班前参数确认" },
        ], async (i) => {
          await api(`/api/stations/${sid}/snapshots`, { method: "POST",
            body: { note: i.note.value, window_minutes: 60 } });
          toast("不可覆盖快照已形成", "ok"); loadShifts();
        }, "生成快照") }, "形成运行快照"),
        el("button", { class: "btn btn-primary", onclick: () => confirmModal("班次交接", [
          { key: "note", label: "交接说明", type: "textarea",
            ph: "运行情况、未竟事项、注意事项" },
        ], async (i) => {
          await api(`/api/stations/${sid}/shifts-close`, { method: "POST",
            body: { note: i.note.value } });
          toast("班次已交接（自动生成末次快照）", "ok"); loadShifts();
        }, "确认交接") }, "交接下班")));
  } else {
    const openBtn = el("button", { class: "btn btn-primary", onclick: () =>
      confirmModal("开班", [{ key: "kind", label: "班次类型", type: "select",
        options: [["day", "白班"], ["night", "夜班"]] }],
        async (i) => { await api(`/api/stations/${sid}/shifts-open`, { method: "POST",
          body: { kind: i.kind.value } }); toast("已开班", "ok"); loadShifts(); },
        "开班") }, "开班");
    active.replaceChildren(
      el("p", { class: "muted" }, "当前无进行中班次"),
      can("dispatcher") ? openBtn : el("span", { class: "meta" }, "值班员可开班"));
  }

  $("#sh-list").replaceChildren(...shifts.map((s) =>
    el("div", { class: "shift-item" },
      el("div", {},
        el("span", { class: "seq" }, `#${s.seq} ${s.kind === "day" ? "白班" : "夜班"}`),
        " ", el("span", { class: "pill" }, s.ended_at ? "已交接" : "进行中"),
        el("span", { class: "meta" },
          `　${s.operator_name}　${fmtTime(s.started_at)} → ${s.ended_at ? fmtTime(s.ended_at) : "现在"}`)),
      s.handover_note ? el("div", { class: "meta" }, "交接说明：" + s.handover_note) : null,
      el("div", { class: "event-actions" },
        el("button", { class: "btn btn-ghost", onclick: async () => {
          // 该班快照（取最近一次）
          const all = await snapshotListFor(s.id);
          if (!all.length) return toast("该班次暂无快照");
          showSnapshot(all[all.length - 1].id);
        } }, "查看快照"),
        el("button", { class: "btn btn-ghost", onclick: () => loadAttachments(s.id) }, "交接附件")))));
}
async function snapshotListFor(shiftId) {
  // 快照接口没有列表端点，通过详情不可列；用通用 events? 简单起见直接请求
  const res = await fetch(`/api/shifts/${shiftId}/snapshots`, {
    headers: { Authorization: "Bearer " + state.token } });
  if (res.ok) return (await res.json()).snapshots;
  return [];
}
async function showSnapshot(snapId) {
  const d = await api("/api/snapshots/" + snapId);
  const box = el("div", {},
    el("h3", {}, `不可覆盖运行快照 #${snapId}`),
    el("p", { class: "muted" },
      `窗口 ${fmtTimeFull(d.snapshot.window_start)} → ${fmtTimeFull(d.snapshot.window_end)}　${d.snapshot.note}`),
    el("table", { class: "table" },
      el("thead", {}, el("tr", {},
        el("th", {}, "测点"), el("th", {}, "平均"), el("th", {}, "最低"),
        el("th", {}, "最高"), el("th", {}, "末次"), el("th", {}, "样本"),
        el("th", {}, "断点数"))),
      el("tbody", {}, ...d.points.map((p) =>
        el("tr", {},
          el("td", {}, p.label),
          el("td", {}, p.avg_value ?? "—"),
          el("td", {}, p.min_value ?? "—"),
          el("td", {}, p.max_value ?? "—"),
          el("td", {}, `${p.latest_value ?? "—"} ${p.unit}`),
          el("td", {}, String(p.sample_count)),
          el("td", {}, String(p.gap_count)))))));
  modalShow(box);
}
async function loadAttachments(shiftId) {
  const { attachments } = await api(`/api/shifts/${shiftId}/attachments`);
  const box = el("div", {},
    el("h3", {}, `班次 #${shiftId} 交接附件`),
    attachments.length ? el("div", {}, ...attachments.map((a) =>
      el("div", { class: "attach-row" },
        el("span", {}, `📎 ${a.filename}　`,
          el("span", { class: "meta" },
            `${a.uploader_name} · ${fmtTime(a.uploaded_at)} · ${a.size}B`)),
        el("a", { class: "btn btn-ghost",
          href: `/api/attachments/${a.id}`, target: "_blank" }, "下载"))))
      : el("p", { class: "muted" }, "暂无附件"),
    can("dispatcher", "manager") ? uploadArea(shiftId) : null,
    el("div", { class: "modal-actions" }, el("button", { class: "btn",
      onclick: modalClose }, "关闭")));
  modalShow(box);
}
function uploadArea(shiftId) {
  const inp = el("input", { type: "file" });
  return el("div", { style: "margin-top:12px" },
    el("label", {}, "上传交接附件（保存在服务器本地目录 data/handover_attachments）"),
    inp,
    el("button", { class: "btn btn-primary", onclick: async () => {
      const f = inp.files[0];
      if (!f) return toast("请先选择文件");
      const buf = await f.arrayBuffer();
      await fetch(`/api/shifts/${shiftId}/attachments`, {
        method: "POST", headers: {
          Authorization: "Bearer " + state.token,
          "X-Filename": encodeURIComponent(f.name),
          "Content-Type": "application/octet-stream",
        }, body: buf });
      toast("附件已上传", "ok"); loadAttachments(shiftId);
    } }, "上传"));
}

// ================= 回访统计 =================
async function loadCallbacks() {
  const [{ stats }, { tickets }] = await Promise.all([
    api("/api/stats/callback"),
    api("/api/tickets?state=reopened"),
  ]);
  const kpi = (k, v, cls = "") => el("div", { class: "kpi " + cls },
    el("div", { class: "k" }, k), el("div", { class: "v" }, v));
  $("#cb-kpis").replaceChildren(
    kpi("回访满意率", stats.rate != null ? `${(stats.rate * 100).toFixed(1)}%` : "—",
      stats.rate >= 0.9 ? "" : "alert"),
    kpi("已回访", stats.total),
    kpi("满意 / 不满意", `${stats.satisfied} / ${stats.dissatisfied}`),
    kpi("不满意重开工单", stats.reopened, stats.reopened ? "alert" : ""));
  drawBars($("#cb-svg"), ["cold", "hot", "leak", "other"].map((c) => {
    const r = stats.by_category.find((x) => x.category === c);
    return { category: c, sat: r?.sat || 0, dis: r?.dis || 0 };
  }));
  $("#cb-bad").replaceChildren(tickets.length
    ? tickets.map((t) => el("div", { class: "ticket-item" },
        el("div", {}, el("strong", {}, `#${t.id} ${t.building_name} ${t.room || ""}`), " ",
          el("span", { class: "pill warning" }, "回访不满意重开")),
        el("div", { class: "meta" }, `${fmtTime(t.created_at)} · ${t.callback_note || ""}`),
        ticketActions(t)))
    : [el("p", { class: "muted" }, "没有不满意重开工单")]);
}

// ================= 通知 =================
async function refreshNotifications() {
  if (!state.token) return;
  try {
    const { notifications } = await api("/api/notifications");
    const n = notifications.length;
    $("#bell-n").textContent = String(n);
    $("#bell-n").classList.toggle("hidden", n === 0);
    $("#bell").onclick = () => {
      const box = el("div", {},
        el("h3", {}, "提醒（越限审批 / 观察窗到期 / 新事件）"),
        el("div", {}, ...notifications.map((x) =>
          el("div", { class: "event-item" },
            el("div", {}, x.message),
            el("div", { class: "meta" }, fmtTime(x.created_at))))),
        el("div", { class: "modal-actions" },
          el("button", { class: "btn", onclick: async () => {
            await api("/api/notifications/read", { method: "POST" });
            modalClose(); refreshNotifications();
          } }, "全部已读"),
          el("button", { class: "btn btn-primary", onclick: modalClose }, "关闭")));
      modalShow(box);
    };
  } catch (e) {}
}

// ---------- 启动 ----------
renderLoginUsers();
(async () => {
  if (state.token) {
    try {
      const { user } = await api("/api/me");
      if (user) { state.user = user; return enterApp(); }
    } catch (e) {}
  }
  $("#login").classList.remove("hidden");
})();
