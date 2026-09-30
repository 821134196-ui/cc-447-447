/* 换热站联动平台前端（原生 JS，无依赖） */
"use strict";

// ---------------- 基础 ----------------
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const state = {
  user: null, stations: [], buildings: [], view: "overview",
  trendSel: new Set([1]), trendMode: "single", trendHours: 12,
  curveBuilding: 1, approvalId: null, timer: null,
};

const KIND_LABEL = { cold: "不热", hot: "过热", noise: "异响", leak: "漏水", other: "其他" };
const STATUS_LABEL = {
  pending_review: "待复核", pending_execution: "待执行", observing: "生效观察",
  finished: "结束", rejected: "已驳回",
};
const EVENT_LABEL = {
  sensor_break: "传感器断点", temp_diff_abnormal: "温差异常",
  pressure_drop: "压力骤降", pump_switch: "水泵切换",
  mass_cold: "集中不热", repeat_repair: "重复报修",
  callback_unsatisfied: "回访不满意",
};
const CMPL_STATUS = { open: "待处置", merged: "已归并", resolved: "已处置" };
const CAT_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7"];

async function api(method, url, body) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (body) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(url, opt);
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 json */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || `HTTP ${res.status}`);
    err.status = res.status;
    err.code = data && data.code;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}
function badge(text, cls) { return `<span class="badge ${cls}">${esc(text)}</span>`; }
function fmtTime(s) { return s ? s.slice(5, 16) : "—"; }
function fmtTimeSec(s) { return s ? s.slice(5, 19) : "—"; }

// ---------------- 登录 / 导航 ----------------
$$(".login-users button").forEach(b => b.addEventListener("click", async () => {
  try {
    const d = await api("POST", "/api/login", { username: b.dataset.user });
    state.user = d.user;
    await bootstrap();
  } catch (e) { $("#login-err").textContent = e.message; }
}));
$("#btn-logout").addEventListener("click", async () => {
  await api("POST", "/api/logout");
  location.reload();
});
$("#nav").addEventListener("click", e => {
  const a = e.target.closest("a[data-view]");
  if (a) render(a.dataset.view);
});

async function bootstrap() {
  $("#login-view").classList.add("hidden");
  $("#app").classList.remove("hidden");
  const d = await api("GET", "/api/stations");
  state.stations = d.stations;
  state.buildings = d.stations[0].buildings;
  state.trendSel = new Set(state.buildings.slice(0, 2).map(b => b.id));
  const u = state.user;
  $("#user-badge").innerHTML =
    `<b>${esc(u.display_name)}</b> <span class="role-tag">${esc(u.role_label)}</span>`;
  applyNavByRole();
  render(u.role === "service" ? "complaints" : "overview");
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(() => { if (!document.hidden) render(state.view, true); }, 10000);
}

function render(view, silent = false) {
  // 角色与可见页面
  const allowed = NAV_BY_ROLE[state.user.role] || NAV_BY_ROLE.dispatcher;
  if (!allowed.includes(view)) view = allowed[0];
  state.view = view;
  $$("#nav a").forEach(a => a.classList.toggle("active", a.dataset.view === view));
  const el = $("#view");
  const fn = VIEWS[view];
  Promise.resolve(fn(el)).catch(e => {
    if (!silent) el.innerHTML = `<div class="alert-box">加载失败：${esc(e.message)}</div>`;
  });
}

// 各角色可见的导航项
const NAV_BY_ROLE = {
  dispatcher: ["overview", "curves", "trend", "approval", "complaints", "handover", "callback"],
  manager:    ["overview", "curves", "trend", "approval", "complaints", "handover", "callback"],
  ops:        ["overview", "curves", "trend", "approval", "complaints", "handover", "callback"],
  service:    ["overview", "complaints", "callback"],
};
function applyNavByRole() {
  const allowed = NAV_BY_ROLE[state.user.role] || [];
  $$("#nav a").forEach(a =>
    a.classList.toggle("hidden", !allowed.includes(a.dataset.view)));
}

// ---------------- SVG 图表工具 ----------------
function svgEl(name, attrs) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}

/** 双面板趋势：上=供回水，下=室外；同一时间轴，各自独立纵轴（小多组，避免双轴）。 */
function renderTrendChart(container, series) {
  container.innerHTML = "";
  const W = Math.max(640, container.clientWidth || 900);
  const H1 = 250, H2 = 90, M = { l: 46, r: 64, t: 14, b: 22 };
  const svg = svgEl("svg", { class: "trend", viewBox: `0 0 ${W} ${H1 + H2 + 26}`,
    width: "100%" });

  const all = series.flatMap(s => s.points);
  if (!all.length) {
    svg.appendChild(svgEl("text", { x: W / 2, y: 120, "text-anchor": "middle",
      class: "axis-label" })).textContent = "所选时间窗内无读数";
    container.appendChild(svg); return;
  }
  const t0 = new Date(all[0].ts.replace(" ", "T"));
  const t1 = new Date(all[all.length - 1].ts.replace(" ", "T"));
  const x = ts => M.l + (new Date(ts.replace(" ", "T")) - t0) / Math.max(1, t1 - t0) *
    (W - M.l - M.r);

  const waterPts = all;
  const supLo = Math.min(...waterPts.map(p => p.supply_temp));
  const supHi = Math.max(...waterPts.map(p => p.supply_temp));
  const retLo = Math.min(...waterPts.map(p => p.return_temp));
  const retHi = Math.max(...waterPts.map(p => p.return_temp));
  const wLo = niceFloor(Math.min(supLo - 2, retLo - 2));
  const wHi = niceCeil(Math.max(supHi + 2, retHi + 2));
  const outLo = Math.min(...all.map(p => p.outdoor_temp)) - 1;
  const outHi = Math.max(...all.map(p => p.outdoor_temp)) + 1;
  const yW = v => H1 - M.b - (v - wLo) / (wHi - wLo) * (H1 - M.t - M.b);
  const yO = v => H1 + H2 + 6 - M.b - (v - outLo) / (outHi - outLo) * (H2 - 8);

  // 网格 + 必要刻度：水面板 3 条
  for (const v of [wLo, (wLo + wHi) / 2, wHi]) {
    svg.appendChild(svgEl("line", { x1: M.l, x2: W - M.r, y1: yW(v), y2: yW(v),
      class: "gridline" }));
    const t = svgEl("text", { x: M.l - 7, y: yW(v) + 3.5, "text-anchor": "end",
      class: "axis-label" });
    t.textContent = v.toFixed(0) + "℃";
    svg.appendChild(t);
  }
  // 室外面板 2 条
  for (const v of [outLo, outHi]) {
    const t = svgEl("text", { x: M.l - 7, y: yO(v) + 3.5, "text-anchor": "end",
      class: "axis-label" });
    t.textContent = v.toFixed(0) + "℃";
    svg.appendChild(t);
  }
  // 时间刻度：最多 5 个
  const tickN = Math.min(5, all.length);
  for (let i = 0; i < tickN; i++) {
    const p = all[Math.round(i * (all.length - 1) / (tickN - 1))];
    const tx = x(p.ts);
    svg.appendChild(svgEl("line", { x1: tx, x2: tx, y1: M.t, y2: H1 + H2 + 6 - M.b,
      class: "gridline", "stroke-dasharray": "2 4", opacity: ".6" }));
    const t = svgEl("text", { x: tx, y: H1 + H2 + 20, "text-anchor": "middle",
      class: "axis-label" });
    t.textContent = p.ts.slice(11, 16);
    svg.appendChild(t);
  }
  // 面板标题（直接标注，替代图例框）
  const lt = svgEl("text", { x: M.l, y: M.t - 2, class: "series-label", fill: "#d03b3b" });
  lt.textContent = "供水/回水温度 ℃"; svg.appendChild(lt);
  const ot = svgEl("text", { x: M.l, y: H1 + 8, class: "series-label", fill: "#8a97a5" });
  ot.textContent = "室外温度 ℃"; svg.appendChild(ot);

  // 数据线
  series.forEach((s, si) => {
    const dim = series.length > 1 ? .85 : 1;
    const mkPath = (key, yy, color, dash) => {
      const d = s.points.map((p, i) =>
        `${i ? "L" : "M"}${x(p.ts).toFixed(1)},${yy(p[key]).toFixed(1)}`).join("");
      svg.appendChild(svgEl("path", { d, fill: "none", stroke: color,
        "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
        "stroke-dasharray": dash || "", opacity: dim }));
    };
    mkPath("supply_temp", yW, "#d03b3b");
    mkPath("return_temp", yW, "#2a78d6");
    mkPath("outdoor_temp", yO, "#8a97a5", "5 4");
    // 直接端标
    const last = s.points[s.points.length - 1];
    if (last) {
      const ls = svgEl("text", { x: W - M.r + 6, y: yW(last.supply_temp) + 3.5,
        class: "series-label", fill: "#d03b3b" });
      ls.textContent = (series.length > 1 ? s.name + "供" : "供 " + last.supply_temp.toFixed(1));
      svg.appendChild(ls);
      if (series.length === 1) {
        const lr = svgEl("text", { x: W - M.r + 6, y: yW(last.return_temp) + 3.5,
          class: "series-label", fill: "#2a78d6" });
        lr.textContent = "回 " + last.return_temp.toFixed(1);
        svg.appendChild(lr);
      }
    }
  });

  // 十字线 + tooltip（hover 层）
  const cross = svgEl("line", { y1: M.t, y2: H1 + H2 - M.b,
    stroke: "#52606e", "stroke-width": 1, "stroke-dasharray": "3 3", opacity: 0 });
  svg.appendChild(cross);
  const tip = document.createElement("div");
  tip.className = "tick-pill";
  const box = document.createElement("div");
  box.className = "chart-box";
  container.appendChild(box);
  box.appendChild(svg); box.appendChild(tip);

  svg.addEventListener("mousemove", e => {
    const rect = svg.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * (W / rect.width);
    let nearest = null, nd = 1e9;
    series.forEach(s => s.points.forEach(p => {
      const dd = Math.abs(x(p.ts) - mx);
      if (dd < nd) { nd = dd; nearest = p; }
    }));
    if (!nearest) return;
    cross.setAttribute("x1", x(nearest.ts)); cross.setAttribute("x2", x(nearest.ts));
    cross.setAttribute("opacity", .7);
    let txt = `${nearest.ts.slice(5, 16)}\n供 ${nearest.supply_temp.toFixed(1)}℃  回 ${nearest.return_temp.toFixed(1)}℃\n室外 ${nearest.outdoor_temp.toFixed(1)}℃`;
    if (series.length > 1) {
      txt = nearest.ts.slice(5, 16) + "\n" + series.map(s => {
        const p = s.points.reduce((a, b) =>
          Math.abs(x(b.ts) - mx) < Math.abs(x(a.ts) - mx) ? b : a);
        return `${s.name} 供${p.supply_temp.toFixed(1)}/回${p.return_temp.toFixed(1)}`;
      }).join("\n");
    }
    tip.textContent = txt;
    tip.style.left = `${(x(nearest.ts) / W * 100)}%`;
    tip.style.top = `${(yW(nearest.supply_temp) / (H1 + H2 + 26) * 100)}%`;
  });
  svg.addEventListener("mouseleave", () => {
    cross.setAttribute("opacity", 0); tip.style.display = "none";
  });
  svg.addEventListener("mouseenter", () => { tip.style.display = ""; });
}

function niceFloor(v) { return Math.floor(v / 5) * 5; }
function niceCeil(v) { return Math.ceil(v / 5) * 5; }

/** 曲线对照小图（旧/新），上下两栏各自独立。 */
function renderCurveMini(container, points, opts = {}) {
  container.innerHTML = "";
  const W = container.clientWidth || 600, H = 180;
  const M = { l: 44, r: 54, t: 16, b: 26 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", class: "trend" });
  const xs = points.map(p => p.outdoor), ys = points.flatMap(p => [p.supply, p.return]);
  const xLo = Math.min(...xs), xHi = Math.max(...xs);
  const yLo = niceFloor(Math.min(...ys) - 3), yHi = niceCeil(Math.max(...ys) + 3);
  const X = v => M.l + (v - xLo) / (xHi - xLo) * (W - M.l - M.r);
  const Y = v => H - M.b - (v - yLo) / (yHi - yLo) * (H - M.t - M.b);
  for (const v of [yLo, (yLo + yHi) / 2, yHi]) {
    svg.appendChild(svgEl("line", { x1: M.l, x2: W - M.r, y1: Y(v), y2: Y(v), class: "gridline" }));
    const t = svgEl("text", { x: M.l - 6, y: Y(v) + 3.5, "text-anchor": "end", class: "axis-label" });
    t.textContent = v.toFixed(0); svg.appendChild(t);
  }
  points.forEach(p => {
    const t = svgEl("text", { x: X(p.outdoor), y: H - 8, "text-anchor": "middle", class: "axis-label" });
    t.textContent = p.outdoor + "℃"; svg.appendChild(t);
  });
  for (const [key, color, label] of [["supply", "#d03b3b", "供"], ["return", "#2a78d6", "回"]]) {
    const d = points.map((p, i) =>
      `${i ? "L" : "M"}${X(p.outdoor).toFixed(1)},${Y(p[key]).toFixed(1)}`).join("");
    svg.appendChild(svgEl("path", { d, fill: "none", stroke: color, "stroke-width": 2.2 }));
    points.forEach(p => svg.appendChild(svgEl("circle",
      { cx: X(p.outdoor), cy: Y(p[key]), r: 3.4, fill: "#fff", stroke: color, "stroke-width": 2 })));
    const last = points[points.length - 1];
    const t = svgEl("text", { x: W - M.r + 6, y: Y(last[key]) + 4, class: "series-label", fill: color });
    t.textContent = label + " " + last[key].toFixed(0); svg.appendChild(t);
  }
  container.appendChild(svg);
}

// ---------------- 视图 1：站点总览 ----------------
async function viewOverview(el) {
  const d = await api("GET", "/api/overview");
  const events = (await api("GET", "/api/events?status=open")).events;
  const density = (await api("GET", "/api/density")).density;
  const densMap = Object.fromEntries(density.map(x => [x.building_id, x]));
  const role = state.user.role;
  const latest = d.latest;
  const isService = role === "service";

  const eventKinds = isService
    ? ["repeat_repair", "callback_unsatisfied"]
    : Object.keys(EVENT_LABEL);
  const counts = Object.fromEntries(eventKinds.map(k => [k, events.filter(e => e.kind === k).length]));

  const statHtml = isService ? `
    ${statCard("未结报修", d.open_complaints, "重复报修自动归并", "red", d.open_complaints ? "warn" : "good")}
    ${statCard("重复报修事件", counts.repeat_repair || 0, "同住户 24h 内追访", "blue", counts.repeat_repair ? "warn" : "good")}
    ${statCard("回访不满意", counts.callback_unsatisfied || 0, "需再次派单处置", "red", counts.callback_unsatisfied ? "warn" : "good")}
    ${statCard("在管楼栋", d.buildings.length, d.station.name, "blue", "good")}
  ` : `
    ${statCard("不可变运行快照", d.snapshot_count, d.chain_ok ? "哈希链完整" : "哈希链异常", d.chain_ok ? "blue" : "red", d.chain_ok ? "good" : "bad")}
    ${statCard("待复核指令", d.commands.pending_review || 0, "越限需经理批准", "warn", (d.commands.pending_review || 0) ? "warn" : "normal")}
    ${statCard("待执行 / 观察中", `${d.commands.pending_execution || 0} / ${d.commands.observing || 0}`, "观察期关联同楼栋报修", "blue", "normal")}
    ${statCard("未结报修", d.open_complaints, "七类事件分开处置", "red", d.open_complaints ? "warn" : "good")}
  `;

  el.innerHTML = `
    <div class="grid grid-4">${statHtml}</div>

    <div class="card">
      <h2>${isService ? "服务事件" : "七类运行 / 服务事件"}
        <span class="sub">${isService ? "重复报修与回访不满意分开处置" : "点击进入对应处置（不同事件处置流程不同）"}</span></h2>
      <div class="event-pills">
        ${eventKinds.map(k => `
          <button class="event-pill ${events.find(e => e.kind === k && e.severity === "critical") ? "critical" : counts[k] ? "warning" : ""}"
                  data-kind="${k}" ${counts[k] ? "" : "style=opacity:.45"}>
            ${EVENT_LABEL[k]} <b>${counts[k]}</b>
          </button>`).join("")}
      </div>
      <div id="event-detail" style="margin-top:14px"></div>
    </div>

    ${isService ? `
    <div class="card">
      <h2>近 24 小时报修密度 <span class="sub">表格内微型条形</span></h2>
      <table><thead><tr><th>楼栋</th><th>户数</th><th class="num">报修数</th><th class="density-bar-cell">密度</th></tr></thead>
      <tbody>${d.buildings.map(b => {
        const dn = densMap[b.id] || { ratio: 0, total: 0 };
        return `<tr><td><b>${esc(b.name)}</b></td><td>${b.households}</td>
          <td class="num">${dn.total || 0}</td>
          <td><span class="mini-bar"><span class="track" style="width:130px">
            <span class="fill" style="width:${(dn.ratio || 0) * 130}px"></span></span></span></td></tr>`;
      }).join("")}</tbody></table>
    </div>` : `
    <div class="card">
      <h2>楼栋实时状态 <span class="sub">红色=供水温度 / 蓝色=回水温度，右侧为近 24h 报修密度</span></h2>
      <div class="bld-row" style="color:var(--muted);font-size:12px;">
        <span>楼栋</span><span>供/回水 ℃</span><span>室外</span><span>供水压</span>
        <span>回水压</span><span>水泵</span><span style="text-align:right">报修密度</span>
      </div>
      ${d.buildings.map(b => {
        const r = latest[b.id];
        const dn = densMap[b.id] || { ratio: 0, total: 0 };
        if (!r) return `<div class="bld-row"><span>${esc(b.name)}</span><span class="muted">暂无读数</span></div>`;
        const pumpBadge = r.pump_status === "running" ? badge("运行", "badge-green")
          : r.pump_status === "switching" ? badge("切换中", "badge-amber") : badge("停止", "badge-gray");
        return `<div class="bld-row">
          <span><b>${esc(b.name)}</b><br><span class="muted" style="font-size:11px">${b.households}户</span></span>
          <span><span class="temp-s">${r.supply_temp.toFixed(1)}</span> / <span class="temp-r">${r.return_temp.toFixed(1)}</span></span>
          <span>${r.outdoor_temp.toFixed(1)}</span>
          <span>${r.supply_pressure.toFixed(2)}</span>
          <span>${r.return_pressure.toFixed(2)}</span>
          <span>${pumpBadge}</span>
          <span class="mini-bar">
            <span class="track"><span class="fill" style="width:${dn.ratio * 100}%"></span></span>
            <span class="n">${dn.total}</span>
          </span>
        </div>`;
      }).join("")}
    </div>`}`;

  $$(".event-pill", el).forEach(btn => btn.addEventListener("click", () =>
    renderEventDetail($("#event-detail"), btn.dataset.kind, events, role)));
  // 默认展示最严重的一类
  const first = eventKinds.find(k => counts[k]) || "sensor_break";
  renderEventDetail($("#event-detail"), first, events, role);
}

function statCard(title, value, sub, color, tone) {
  const cls = tone === "warn" ? "warn" : tone === "good" ? "blue" : color;
  return `<div class="card" style="margin-bottom:0">
    <div class="stat"><span class="k">${esc(title)}</span>
    <span class="v ${tone === "warn" ? "warn" : color === "red" ? "red" : "blue"}">${value}</span>
    <span class="muted" style="font-size:12px">${esc(sub)}</span></div></div>`;
}

async function renderEventDetail(container, kind, events, role) {
  const list = events.filter(e => e.kind === kind);
  const guide = {
    sensor_break: "核实通讯与供电；数据恢复后由运维执行断点补录，事件自动关闭。",
    temp_diff_abnormal: "检查板换与二网平衡；温差恢复后事件自动关闭，也可人工说明关闭。",
    pressure_drop: "排查泄漏与补水；现场消缺后人工关闭并记录。",
    pump_switch: "确认备用泵运行参数，登记切换原因后关闭。",
    mass_cold: "升级站点级调度：核查首站出力与平衡阀，统一处置后关闭。",
    repeat_repair: "联系住户二次上门，根因解决并回访满意后关闭。",
    callback_unsatisfied: "重新派单处置，处置完成后再次回访。",
  }[kind];
  container.innerHTML = `
    <div class="alert-box warn" style="margin-bottom:10px">
      <b>${EVENT_LABEL[kind]}</b> · 处置流程：${esc(guide)}
    </div>
    ${list.length === 0 ? `<p class="muted">当前无未关闭的${EVENT_LABEL[kind]}事件。</p>` :
      `<table><thead><tr><th>发生时间</th><th>楼栋</th><th>详情</th><th></th></tr></thead><tbody>
      ${list.map(e => `<tr>
        <td>${fmtTimeSec(e.started_at)}</td>
        <td>${e.building_id ? e.building_id + " 号楼" : "全站点"}</td>
        <td>${esc(eventDetailText(e))}</td>
        <td style="text-align:right">
          ${kind === "sensor_break" && ["ops", "manager"].includes(role)
            ? `<button class="btn btn-sm btn-primary" data-backfill="${e.id}">断点补录</button>` : ""}
          ${["ops", "dispatcher", "manager"].includes(role)
            ? `<button class="btn btn-sm" data-close="${e.id}">处置关闭</button>` : ""}
        </td></tr>`).join("")}
      </tbody></table>`}`;
  $$("button[data-close]", container).forEach(b => b.addEventListener("click", () =>
    openCloseModal(Number(b.dataset.close), kind)));
  $$("button[data-backfill]", container).forEach(b => b.addEventListener("click", () =>
    openBackfillModal(Number(b.dataset.backfill), events, kind)));
}

function eventDetailText(e) {
  const d = e.detail || {};
  switch (e.kind) {
    case "sensor_break": return `数据自 ${fmtTime(d.from)} 中断至 ${fmtTime(d.to)}，间隔 ${d.gap_minutes} 分钟`;
    case "temp_diff_abnormal": return `供 ${d.supply}℃ / 回 ${d.return}℃，温差仅 ${d.diff}℃`;
    case "pressure_drop": return `供水压由 ${d.before} 降至 ${d.after} MPa，骤降 ${d.drop}`;
    case "pump_switch": return `${pumpLabel(d.from)} → ${pumpLabel(d.to)}，频率 ${d.frequency}Hz`;
    case "mass_cold": return `${d.window?.[0]?.slice(5, 16)} 起 60 分钟内 ${d.distinct_buildings} 栋楼报不热`;
    case "repeat_repair": return `主报修 #${d.root_complaint} 已被追访 ${d.count ?? 2} 次`;
    case "callback_unsatisfied": return `报修 #${d.complaint}：${esc(d.note || "住户不满意")}`;
    default: return "";
  }
}
function pumpLabel(s) { return { running: "运行", stopped: "停止", switching: "切换中" }[s] || s; }

function openCloseModal(eventId, kind) {
  modal(`关闭事件：${EVENT_LABEL[kind]}`, `
    <div class="field"><label>处置说明</label>
      <textarea id="m-note" rows="3" placeholder="记录处置过程与结果"></textarea></div>
  `, [
    { text: "取消", onClick: closeModal },
    { text: "确认关闭", primary: true, onClick: async () => {
      try {
        await api("POST", `/api/events/${eventId}/close`, { note: $("#m-note").value });
        closeModal(); toast("事件已关闭", "ok"); render(state.view);
      } catch (e) { toast(e.message, "err"); }
    } },
  ]);
}

function openBackfillModal(eventId, events, kind) {
  const ev = events.find(e => e.id === eventId);
  modal("断点补录", `
    <p class="muted" style="margin-top:0">补录数据写入只读历史（标记为 backfill），落在断点窗口内将自动关闭该事件；既有快照不受影响。</p>
    <div class="grid grid-2">
      <div class="field"><label>时间（YYYY-MM-DD HH:MM:SS）</label>
        <input id="bf-ts" value="${ev.detail.from}"></div>
      <div class="field"><label>室外温度 ℃</label><input id="bf-o" type="number" value="-7.5"></div>
      <div class="field"><label>供水温度 ℃</label><input id="bf-s" type="number" value="51.0"></div>
      <div class="field"><label>回水温度 ℃</label><input id="bf-r" type="number" value="41.0"></div>
      <div class="field"><label>供水压力 MPa</label><input id="bf-sp" type="number" step=".01" value="0.62"></div>
      <div class="field"><label>回水压力 MPa</label><input id="bf-rp" type="number" step=".01" value="0.28"></div>
    </div>
  `, [
    { text: "取消", onClick: closeModal },
    { text: "提交补录", primary: true, onClick: async () => {
      try {
        const v = id => Number($(id).value);
        const d = await api("POST", "/api/readings/backfill", {
          station_id: 1, building_id: ev.building_id, ts: $("#bf-ts").value,
          outdoor_temp: v("#bf-o"), supply_temp: v("#bf-s"), return_temp: v("#bf-r"),
          supply_pressure: v("#bf-sp"), return_pressure: v("#bf-rp"),
          pump_status: "running", pump_frequency: 38,
        });
        closeModal();
        toast(d.closed_events.length ? "补录成功，断点事件已自动关闭" : "补录成功", "ok");
        render(state.view);
      } catch (e) { toast(e.message, "err"); }
    } },
  ]);
}

// ---------------- 视图 2：曲线编辑 ----------------
async function viewCurves(el) {
  const role = state.user.role;
  const canEdit = ["dispatcher", "manager"].includes(role);
  const d = await api("GET", `/api/curve?station_id=1&building_id=${state.curveBuilding}`);
  const cur = d.current;
  const points = cur ? cur.points.map(p => ({ ...p })) : [];

  el.innerHTML = `
    <div class="card">
      <h2>供暖目标曲线编辑 <span class="sub">值班员依据室外温度提交新目标曲线；版本冲突时后提交者将收到提示</span></h2>
      <div class="toolbar">
        <select id="cv-building" style="width:150px">
          ${state.buildings.map(b => `<option value="${b.id}" ${b.id === state.curveBuilding ? "selected" : ""}>${esc(b.name)}</option>`).join("")}
        </select>
        <span class="muted">当前版本：<b id="cv-ver">v${cur ? cur.version : 0}</b>
          · 越限区间：供水 [38,75]℃ 回水 [30,55]℃ 温差 [6,25]℃</span>
      </div>
      ${canEdit ? "" : `<div class="alert-box warn">当前角色（${esc(state.user.role_label)}）仅可查看曲线，不能编辑。</div>`}
      <div id="cv-conflict"></div>
      <div class="grid grid-2">
        <div>
          <table class="curve-table">
            <thead><tr><th>室外温度 ℃</th><th>目标供水 ℃</th><th>目标回水 ℃</th>${canEdit ? "<th></th>" : ""}</tr></thead>
            <tbody id="cv-rows">
              ${points.map((p, i) => cvRow(p, i, canEdit)).join("")}
            </tbody>
          </table>
          ${canEdit ? `<button class="btn btn-sm" id="cv-add" style="margin-top:8px">+ 添加折点</button>` : ""}
          <p class="limit-hint" style="margin-top:8px">折点按室外温度升序；供水必须高于回水。提交即生成新版本（历史版本不覆盖）。</p>
          ${canEdit ? `<div class="toolbar" style="margin-top:6px">
            <input id="cv-note" placeholder="调整说明（如：寒潮上调）" style="max-width:280px">
            <div class="spacer"></div>
            <button class="btn btn-primary" id="cv-save">提交新版本（基于 v${cur ? cur.version : 0}）</button>
          </div>` : ""}
        </div>
        <div>
          <div class="split-head"><h3>曲线形状</h3><span class="muted">红=供水 蓝=回水</span></div>
          <div id="cv-chart"></div>
        </div>
      </div>
    </div>
    <div class="card">
      <h2>版本历史 <span class="sub">两名值班员修改同一版本时，后提交者将被拒绝并提示合并</span></h2>
      <table>
        <thead><tr><th>版本</th><th>提交人</th><th>提交时间</th><th>基于</th><th>说明</th></tr></thead>
        <tbody>${d.versions.map(v => `<tr>
          <td><b>v${v.version}</b></td>
          <td>${esc(v.author_name)}</td>
          <td>${fmtTimeSec(v.created_at)}</td>
          <td>${v.base_version ? "v" + v.base_version : "—"}</td>
          <td>${esc(v.note || "")}</td></tr>`).join("")}</tbody>
      </table>
    </div>`;

  $("#cv-building").addEventListener("change", e => {
    state.curveBuilding = Number(e.target.value); render("curves");
  });

  function collect() {
    return $$("#cv-rows tr").map(tr => ({
      outdoor: Number(tr.querySelector(".p-o").value),
      supply: Number(tr.querySelector(".p-s").value),
      return: Number(tr.querySelector(".p-r").value),
    }));
  }
  function redraw() {
    try { renderCurveMini($("#cv-chart"), collect()); $("#cv-conflict").innerHTML = ""; }
    catch (e) { /* 中间态不绘制 */ }
  }
  $("#cv-rows").addEventListener("input", redraw);
  if (canEdit) {
    $("#cv-add").addEventListener("click", () => {
      const tbody = $("#cv-rows");
      const i = tbody.children.length;
      tbody.insertAdjacentHTML("beforeend", cvRow({ outdoor: 0, supply: 44, return: 36 }, i, true));
      redraw();
    });
    $("#cv-rows").addEventListener("click", e => {
      if (e.target.classList.contains("x-btn")) {
        e.target.closest("tr").remove(); redraw();
      }
    });
    $("#cv-save").addEventListener("click", async () => {
      try {
        const res = await api("POST", "/api/curve", {
          station_id: 1, building_id: state.curveBuilding,
          points: collect(), base_version: cur.version, note: $("#cv-note").value,
        });
        if (res.over_limit_details.length) {
          toast("新版本已保存，但含越限折点：下达指令需经理批准", "err");
        } else {
          toast(`曲线 v${res.curve.version} 已提交`, "ok");
        }
        render("curves");
      } catch (e) {
        if (e.status === 409) {
          $("#cv-conflict").innerHTML = `<div class="conflict-box">
            <b>⚠ 版本冲突</b>
            <p style="margin:6px 0">${esc(e.message)}</p>
            <button class="btn btn-sm btn-primary" id="cv-refresh">刷新到最新版本并合并我的修改</button>
          </div>`;
          $("#cv-refresh").addEventListener("click", () => render("curves"));
        } else toast(e.message, "err");
      }
    });
  }
  redraw();
}
function cvRow(p, i, canEdit) {
  return `<tr>
    <td><input class="p-o" type="number" value="${p.outdoor}" ${canEdit ? "" : "disabled"}></td>
    <td><input class="p-s" type="number" value="${p.supply}" ${canEdit ? "" : "disabled"}></td>
    <td><input class="p-r" type="number" value="${p.return}" ${canEdit ? "" : "disabled"}></td>
    ${canEdit ? '<td><button class="x-btn" title="删除">×</button></td>' : ""}
  </tr>`;
}

// ---------------- 视图 3：实时趋势 ----------------
async function viewTrend(el) {
  el.innerHTML = `
    <div class="card">
      <h2>实时趋势 <span class="sub">按楼栋叠加比较；仅保留必要刻度</span></h2>
      <div class="toolbar">
        ${state.buildings.map(b => `
          <label style="margin:0;display:flex;align-items:center;gap:5px;cursor:pointer">
            <input type="checkbox" style="width:auto" data-bid="${b.id}"
              ${state.trendSel.has(b.id) ? "checked" : ""}> ${esc(b.name)}</label>`).join("")}
        <div class="spacer"></div>
        <select id="tr-hours" style="width:110px">
          ${[6, 12, 24, 48].map(h => `<option value="${h}" ${h === state.trendHours ? "selected" : ""}>近 ${h} 小时</option>`).join("")}
        </select>
      </div>
      <div class="chart-legend">
        <span class="lg"><span class="sw" style="border-color:#d03b3b"></span>供水温度</span>
        <span class="lg"><span class="sw" style="border-color:#2a78d6"></span>回水温度</span>
        <span class="lg"><span class="sw" style="border-color:#8a97a5;border-top-style:dashed"></span>室外温度</span>
      </div>
      <div id="tr-chart" class="chart-wrap"></div>
    </div>`;
  $$('input[data-bid]', el).forEach(cb => cb.addEventListener("change", () => {
    const id = Number(cb.dataset.bid);
    cb.checked ? state.trendSel.add(id) : state.trendSel.delete(id);
    loadTrend();
  }));
  $("#tr-hours").addEventListener("change", e => {
    state.trendHours = Number(e.target.value); loadTrend();
  });
  async function loadTrend() {
    const ids = [...state.trendSel].sort();
    if (!ids.length) { $("#tr-chart").innerHTML = `<p class="muted">请至少选择一栋楼</p>`; return; }
    const d = await api("GET", `/api/trend?buildings=${ids.join(",")}&hours=${state.trendHours}`);
    const series = ids.map(id => d.series[id]);
    renderTrendChart($("#tr-chart"), series);
  }
  loadTrend();
}

// ---------------- 视图 4：指令审批（上下分栏）----------------
async function viewApproval(el) {
  const role = state.user.role;
  const d = await api("GET", "/api/commands");
  const cmds = d.commands;
  const filters = [
    ["pending_review", "待复核"], ["pending_execution", "待执行"],
    ["observing", "生效观察"], ["finished", "已结束"], ["rejected", "已驳回"],
  ];
  el.innerHTML = `
    <div class="card">
      <h2>调节指令 <span class="sub">待复核 → 待执行 → 生效观察 → 结束；越限指令（红框）必须经理批准</span></h2>
      <div class="toolbar">
        ${filters.map(([k, label], i) => `
          <button class="btn btn-sm ${i === 0 ? "btn-primary" : ""}" data-filter="${k}">
            ${label}（${cmds.filter(c => c.status === k).length}）</button>`).join("")}
      </div>
      <div id="cmd-list"></div>
    </div>
    <div id="cmd-detail-wrap"></div>`;

  let filter = "pending_review";
  function listHtml() {
    const rows = cmds.filter(c => c.status === filter);
    if (!rows.length) return `<p class="muted">没有「${STATUS_LABEL[filter]}」的指令。</p>`;
    return `<table><thead><tr><th>指令</th><th>楼栋</th><th>版本变化</th><th>提交人/时间</th><th>越限</th><th></th></tr></thead><tbody>
      ${rows.map(c => `<tr>
        <td>#${c.id} ${badge(STATUS_LABEL[c.status],
          c.status === "pending_review" ? "badge-amber" : c.status === "observing" ? "badge-blue"
          : c.status === "finished" ? "badge-green" : "badge-gray")}</td>
        <td>${c.building_id} 号楼</td>
        <td>v${c.old_curve_version ?? "—"} → <b>v${c.new_curve_version}</b></td>
        <td>${esc(c.submitted_name)}<br><span class="muted" style="font-size:11px">${fmtTime(c.submitted_at)}</span></td>
        <td>${c.over_limit ? badge("越限", "badge-red") : badge("正常", "badge-green")}</td>
        <td style="text-align:right"><button class="btn btn-sm btn-primary" data-cid="${c.id}">查看对照</button></td>
      </tr>`).join("")}</tbody></table>`;
  }
  function paintList() {
    $("#cmd-list").innerHTML = listHtml();
    $$("button[data-cid]", $("#cmd-list")).forEach(b => b.addEventListener("click", () =>
      paintDetail(Number(b.dataset.cid))));
  }
  $$("button[data-filter]", el).forEach(b => b.addEventListener("click", () => {
    filter = b.dataset.filter;
    $$("button[data-filter]", el).forEach(x =>
      x.classList.toggle("btn-primary", x === b));
    paintList();
  }));
  paintList();
  if (state.approvalId) paintDetail(state.approvalId).catch(() => {});

  async function paintDetail(cid) {
    state.approvalId = cid;
    const d = await api("GET", `/api/commands/${cid}/curves`);
    const c = d.command;
    const canReview = ["dispatcher", "manager"].includes(role);
    const canApproveOver = role === "manager";
    const canExecute = ["ops", "manager"].includes(role);
    const canFinish = ["dispatcher", "manager"].includes(role);
    const observeLeft = c.status === "observing" && c.observe_until
      ? Math.max(0, (new Date(c.observe_until.replace(" ", "T")) - Date.now()) / 60000) : 0;

    $("#cmd-detail-wrap").innerHTML = `
      <div class="cmd-card ${c.over_limit ? "over" : ""}">
        <div class="split-head">
          <h3>指令 #${c.id} · ${c.building_id} 号楼</h3>
          ${badge(STATUS_LABEL[c.status], c.status === "pending_review" ? "badge-amber" : "badge-blue")}
          ${c.over_limit ? badge("越限 · 须经理批准", "badge-red") : ""}
        </div>
        <div class="cmd-meta">
          <span>提交：${esc(c.submitted_name)} · ${fmtTimeSec(c.submitted_at)}</span>
          ${c.reviewed_name ? `<span>复核：${esc(c.reviewed_name)} · ${fmtTimeSec(c.reviewed_at)}（${esc(c.review_note)}）</span>` : ""}
          ${c.executed_name ? `<span>执行：${esc(c.executed_name)} · ${fmtTimeSec(c.executed_at)}</span>` : ""}
          ${c.observe_until ? `<span>观察期至 ${fmtTimeSec(c.observe_until)}${observeLeft > 0 ? `（剩 ${Math.ceil(observeLeft)} 分钟）` : "（已到）"}</span>` : ""}
        </div>
        ${c.over_limit ? `<div class="alert-box"><b>越限明细：</b>${esc(c.limit_detail)}</div>` : ""}
        ${c.effect_note ? `<div class="alert-box warn" style="margin-bottom:0"><b>效果结论：</b>${esc(c.effect_note)}</div>` : ""}
        <div class="cmd-actions">
          ${c.status === "pending_review" ? `
            ${c.over_limit && !canApproveOver ? `<span class="muted">越限指令仅供热经理可批准（当前角色：${esc(state.user.role_label)}）</span>` : ""}
            ${canReview && (!c.over_limit || canApproveOver) ? `
              <button class="btn btn-primary" data-act="review" data-v="1">${c.over_limit ? "经理批准" : "复核通过"}</button>
              <button class="btn btn-danger" data-act="review" data-v="0">驳回</button>` : ""}
          ` : ""}
          ${c.status === "pending_execution" ? (canExecute
            ? `<button class="btn btn-primary" data-act="execute">运维执行（进入观察期）</button>`
            : `<span class="muted">仅运维可执行（当前角色：${esc(state.user.role_label)}）</span>`) : ""}
          ${c.status === "observing" ? (canFinish
            ? `<button class="btn btn-primary" data-act="finish" ${observeLeft > 0 ? "disabled title=观察期未结束" : ""}>结束观察并判定效果</button>
               <span class="muted">${observeLeft > 0 ? `观察期剩余 ${Math.ceil(observeLeft)} 分钟` : "观察期已到，可结束"}</span>`
            : `<span class="muted">仅值班员可结束观察</span>`) : ""}
        </div>
      </div>

      <div class="split-top">
        <div class="split-head"><h3>① 旧曲线 v${c.old_curve_version ?? "—"}</h3>
          <span class="muted">对照基准${c.old_curve_version ? "" : "（首次下达）"}</span></div>
        <div id="split-old">${d.old ? "" : `<p class="muted">无旧版本</p>`}</div>
        ${d.old ? pointTable(d.old.points, true) : ""}
      </div>
      <div class="split-top new">
        <div class="split-head"><h3>② 新曲线 v${c.new_curve_version}</h3>
          <span class="muted">执行后目标值</span>${c.over_limit ? badge("含越限折点", "badge-red") : ""}</div>
        <div id="split-new"></div>
        ${pointTable(d.new.points, false, d.old ? d.old.points : null)}
      </div>

      <div class="card" style="margin-bottom:0">
        <h2>观察期关联报修 <span class="sub">同楼栋冷暖报修用于判断调节效果</span></h2>
        ${d.linked_complaints.length === 0 ? `<p class="muted">观察期内暂无同楼栋冷暖报修。</p>` :
          `<table><thead><tr><th>时间</th><th>类型</th><th>内容</th><th>状态</th></tr></thead><tbody>
           ${d.linked_complaints.map(x => `<tr>
             <td>${fmtTime(x.created_at)}</td>
             <td class="kind-${x.kind}">${KIND_LABEL[x.kind]}</td>
             <td>${esc(x.content)}</td>
             <td>${CMPL_STATUS[x.status]}</td></tr>`).join("")}
           </tbody></table>`}
      </div>`;

    if (d.old) renderCurveMini($("#split-old"), d.old.points);
    renderCurveMini($("#split-new"), d.new.points);

    $$("button[data-act]", $("#cmd-detail-wrap")).forEach(b => b.addEventListener("click", async () => {
      const act = b.dataset.act;
      try {
        if (act === "review") {
          const approve = b.dataset.v === "1";
          if (!approve) {
            const note = await promptModal("驳回原因");
            if (note === null) return;
            await api("POST", `/api/commands/${cid}/review`, { approve: false, note });
          } else {
            await api("POST", `/api/commands/${cid}/review`,
              { approve: true, note: c.over_limit ? "经理批准越限" : "复核通过" });
          }
          toast(approve ? "已通过，进入待执行" : "已驳回", "ok");
        } else if (act === "execute") {
          await api("POST", `/api/commands/${cid}/execute`, {});
          toast("已执行，进入 90 分钟生效观察期", "ok");
        } else if (act === "finish") {
          await api("POST", `/api/commands/${cid}/finish`, {});
          toast("观察结束，效果结论已生成", "ok");
        }
        render("approval");
      } catch (e) { toast(e.message, "err"); }
    }));
  }
}

function pointTable(points, old, oldPoints = null) {
  const at = (ps, o) => ps.reduce((a, b) =>
    Math.abs(b.outdoor - o) < Math.abs(a.outdoor - o) ? b : a);
  return `<table style="margin-top:10px"><thead><tr>
    <th>室外 ℃</th><th class="num">目标供水 ℃</th><th class="num">目标回水 ℃</th>
    ${oldPoints ? "<th class='num'>供水变化</th><th class='num'>回水变化</th>" : ""}
  </tr></thead><tbody>
  ${points.map(p => {
    const o = oldPoints ? at(oldPoints, p.outdoor) : null;
    const ds = o ? p.supply - o.supply : 0, dr = o ? p.return - o.return : 0;
    const arrow = v => v > 0.01 ? `<span style="color:var(--supply)">▲${v.toFixed(1)}</span>`
      : v < -0.01 ? `<span style="color:var(--return)">▼${(-v).toFixed(1)}</span>` : "0";
    return `<tr${old ? ' style="opacity:.65"' : ""}>
      <td>${p.outdoor}</td>
      <td class="num" style="color:var(--supply)">${p.supply}</td>
      <td class="num" style="color:var(--return)">${p.return}</td>
      ${oldPoints ? `<td class="num">${arrow(ds)}</td><td class="num">${arrow(dr)}</td>` : ""}
    </tr>`;
  }).join("")}</tbody></table>`;
}

// ---------------- 视图 5：报修聚合 ----------------
async function viewComplaints(el) {
  const role = state.user.role;
  const canCreate = ["service", "dispatcher", "manager"].includes(role);
  const [cd, dd] = await Promise.all([
    api("GET", "/api/complaints?station_id=1"),
    api("GET", "/api/density"),
  ]);
  const maxTotal = Math.max(1, ...dd.density.map(x => x.total));
  el.innerHTML = `
    <div class="card">
      <h2>近 24 小时报修密度 <span class="sub">表格内微型条形；红色段为不热报修</span></h2>
      <table>
        <thead><tr><th>楼栋</th><th class="num">报修总数</th><th class="num">其中不热</th>
          <th class="num">待处置</th><th class="density-bar-cell">密度</th></tr></thead>
        <tbody>${dd.density.map(x => {
          const w = Math.round(x.total / maxTotal * 130);
          const coldW = x.total ? Math.round((x.cold || 0) / x.total * Math.max(6, w)) : 0;
          return `<tr>
            <td><b>${esc(x.building_name)}</b></td>
            <td class="num">${x.total}</td>
            <td class="num kind-cold">${x.cold || 0}</td>
            <td class="num">${x.open_n || 0}</td>
            <td><span class="mini-bar"><span class="track" style="width:130px">
              <span class="fill" style="width:${w}px;display:inline-block;position:relative">
                <span style="position:absolute;left:0;top:0;bottom:0;width:${coldW}px;background:var(--supply);border-radius:6px 0 0 6px"></span>
              </span></span></span></td>
          </tr>`;
        }).join("")}</tbody>
      </table>
    </div>
    <div class="card">
      <h2>报修工单
        ${canCreate ? `<button class="btn btn-sm btn-primary" id="cm-new" style="margin-left:auto">登记报修</button>` : ""}
      </h2>
      <table>
        <thead><tr><th>时间</th><th>楼栋/房号</th><th>住户</th><th>联系电话</th>
          <th>类型</th><th>内容</th><th>状态</th><th>关联指令</th><th></th></tr></thead>
        <tbody>${cd.complaints.map(c => `
          <tr${c.status === "merged" ? ' style="opacity:.6"' : ""}>
            <td style="white-space:nowrap">${fmtTime(c.created_at)}</td>
            <td>${c.building_name}<br><span class="muted" style="font-size:11px">${esc(c.room || "—")}</span></td>
            <td>${esc(c.resident_name || "匿名")}</td>
            <td>${esc(c.resident_phone || "—")}</td>
            <td class="kind-${c.kind}">${KIND_LABEL[c.kind]}</td>
            <td style="max-width:260px">${esc(c.content)}
              ${c.status === "merged" ? `<br><span class="merged-note">↳ 已归并至 #${c.merged_into}</span>` : ""}</td>
            <td>${badge(CMPL_STATUS[c.status],
              c.status === "open" ? "badge-red" : c.status === "merged" ? "badge-gray" : "badge-green")}</td>
            <td>${c.command_id ? badge("#" + c.command_id + " 观察期", "badge-blue") : "—"}</td>
            <td style="white-space:right;white-space:nowrap">
              ${c.status === "open" && ["ops", "dispatcher", "manager"].includes(role)
                ? `<button class="btn btn-sm" data-resolve="${c.id}">处置</button>` : ""}
              ${c.status === "resolved" && ["service", "dispatcher", "manager"].includes(role)
                ? `<button class="btn btn-sm" data-cb="${c.id}">回访</button>` : ""}
            </td></tr>`).join("")}
        </tbody>
      </table>
    </div>`;

  if (canCreate) $("#cm-new").addEventListener("click", openNewComplaint);
  $$("button[data-resolve]", el).forEach(b => b.addEventListener("click", async () => {
    try { await api("POST", `/api/complaints/${b.dataset.resolve}/resolve`, {});
      toast("已标记处置完成，可进行回访", "ok"); render("complaints");
    } catch (e) { toast(e.message, "err"); }
  }));
  $$("button[data-cb]", el).forEach(b => b.addEventListener("click", () => openCallbackModal(Number(b.dataset.cb))));
}

function openNewComplaint() {
  modal("登记报修", `
    <div class="grid grid-2">
      <div class="field"><label>楼栋</label>
        <select id="nc-b">${state.buildings.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join("")}</select></div>
      <div class="field"><label>类型</label>
        <select id="nc-kind">
          ${Object.entries(KIND_LABEL).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}
        </select></div>
      <div class="field" style="grid-column:1/-1"><label>报修内容</label>
        <textarea id="nc-content" rows="3" placeholder="住户描述"></textarea></div>
      <div class="field"><label>来源</label>
        <select id="nc-source"><option value="phone">电话</option><option value="app">APP</option><option value="patrol">巡检</option></select></div>
    </div>
    <p class="muted" style="font-size:12px">同住户同楼栋 24h 内再次报修将自动归并；60 分钟内 3 栋楼报不热自动升级为集中不热事件。</p>
  `, [
    { text: "取消", onClick: closeModal },
    { text: "提交", primary: true, onClick: async () => {
      try {
        const d = await api("POST", "/api/complaints", {
          station_id: 1, building_id: Number($("#nc-b").value),
          kind: $("#nc-kind").value, content: $("#nc-content").value,
          source: $("#nc-source").value,
        });
        closeModal();
        if (d.merged_into) toast(`重复报修，已归并至 #${d.merged_into.id}`, "");
        else if (d.mass_cold) toast("已登记，并触发「集中不热」站点级事件", "err");
        else toast("报修已登记", "ok");
        render("complaints");
      } catch (e) { toast(e.message, "err"); }
    } },
  ]);
}

function openCallbackModal(cid) {
  modal(`报修 #${cid} 处置回访`, `
    <div class="field"><label>回访结果</label>
      <select id="cb-result">
        <option value="satisfied">满意</option>
        <option value="unsatisfied">不满意（重新生成事件）</option>
      </select></div>
    <div class="field"><label>回访备注</label><textarea id="cb-note" rows="3"></textarea></div>
  `, [
    { text: "取消", onClick: closeModal },
    { text: "提交回访", primary: true, onClick: async () => {
      try {
        await api("POST", `/api/complaints/${cid}/callback`, {
          result: $("#cb-result").value, note: $("#cb-note").value,
        });
        toast("回访已记录", "ok"); closeModal(); render("complaints");
      } catch (e) { toast(e.message, "err"); }
    } },
  ]);
}

// ---------------- 视图 6：班次交接 ----------------
async function viewHandover(el) {
  const role = state.user.role;
  const canOp = ["dispatcher", "manager"].includes(role);
  const [sd, spd, hd] = await Promise.all([
    api("GET", "/api/shifts?station_id=1"),
    api("GET", "/api/snapshots"),
    api("GET", "/api/handovers?station_id=1"),
  ]);
  const active = sd.active;
  el.innerHTML = `
    <div class="grid grid-2">
      <div class="card" style="margin-bottom:16px">
        <h2>当前班次</h2>
        ${active ? `<p>班次 #${active.id} · <b>${esc(active.leader)}</b> 班
          <br><span class="muted">${fmtTimeSec(active.started_at)} 起 · ${esc(active.note || "")}</span></p>
          ${canOp ? `<button class="btn btn-primary" id="hd-snap">接收读数并形成运行快照</button>
          <p class="limit-hint">快照只增不可改（数据库 UPDATE/DELETE 触发器 + SHA-256 哈希链）。</p>` : ""}`
        : `<p class="muted">无进行中班次</p>`}
        <h2 style="margin-top:18px">历史班次</h2>
        <table><thead><tr><th>班次</th><th>日期</th><th>班长</th><th>起</th><th>止</th></tr></thead><tbody>
        ${sd.shifts.slice(0, 8).map(s => `<tr>
          <td>#${s.id}</td><td>${esc(s.shift_date)}</td><td>${esc(s.leader)}</td>
          <td>${fmtTime(s.started_at)}</td><td>${s.ended_at ? fmtTime(s.ended_at) : badge("进行中", "badge-green")}</td>
        </tr>`).join("")}</tbody></table>
      </div>
      <div class="card" style="margin-bottom:16px">
        <h2>本班次不可变快照</h2>
        <div class="timeline">
          ${spd.snapshots.slice(0, 8).map(s => `
            <div class="tl-item">
              <b>快照 #${s.id}</b> <span class="muted">班次 #${s.shift_id} · ${fmtTimeSec(s.ts)}</span>
              <div class="muted" style="font-size:12px">读数 ${s.reading_count} 条（${fmtTime(s.readings_from)} → ${fmtTime(s.readings_to)}）
                <br>供 ${s.supply_temp}℃ / 回 ${s.return_temp}℃</div>
              <div class="snap-hash">sha256:${s.hash.slice(0, 24)}… ← ${s.prev_hash.slice(0, 12)}…</div>
            </div>`).join("")}
        </div>
      </div>
    </div>
    <div class="card">
      <h2>班次交接 <span class="sub">交接附件保存在服务器本地目录 data/handover/</span>
        ${canOp && active ? `<button class="btn btn-sm btn-primary" id="hd-new" style="margin-left:auto">新建交接</button>` : ""}
      </h2>
      <table>
        <thead><tr><th>时间</th><th>交班 → 接班</th><th>记录人</th><th>交接摘要</th><th>附件</th></tr></thead>
        <tbody>${hd.handovers.map(h => `<tr>
          <td style="white-space:nowrap">${fmtTimeSec(h.created_at)}</td>
          <td>${esc(h.outgoing_leader)} → <b>${esc(h.incoming_leader)}</b></td>
          <td>${esc(h.creator)}</td>
          <td style="max-width:380px">${esc(h.summary)}</td>
          <td>${h.attachment_name ? `<a href="/api/handovers/${h.id}/attachment">${esc(h.attachment_name)} ⬇</a>` : "—"}</td>
        </tr>`).join("")}</tbody>
      </table>
    </div>`;

  if (active && canOp) {
    $("#hd-snap")?.addEventListener("click", async () => {
      try { const d = await api("POST", `/api/shifts/${active.id}/snapshot`);
        toast(`快照 #${d.snapshot.id} 已形成（${d.snapshot.reading_count} 条读数）`, "ok");
        render("handover");
      } catch (e) { toast(e.message, "err"); }
    });
    $("#hd-new")?.addEventListener("click", openHandoverModal);
  }
}

function openHandoverModal() {
  modal("新建班次交接", `
    <div class="grid grid-2">
      <div class="field"><label>接班人</label><input id="ho-to" value="李雪"></div>
      <div class="field"><label>交接附件（本地保存）</label><input id="ho-file" type="file"></div>
    </div>
    <div class="field"><label>交接摘要</label>
      <textarea id="ho-summary" rows="4" placeholder="运行情况、未结报修、待办指令、设备异常…"></textarea></div>
  `, [
    { text: "取消", onClick: closeModal },
    { text: "保存交接", primary: true, onClick: async () => {
      const f = $("#ho-file").files[0];
      let b64 = "", name = "";
      const submit = () => api("POST", "/api/handovers", {
        shift_id: state._activeId, station_id: 1,
        incoming_leader: $("#ho-to").value, summary: $("#ho-summary").value,
        attachment_name: name, attachment_base64: b64,
      });
      try {
        const sd = await api("GET", "/api/shifts?station_id=1");
        state._activeId = sd.active.id;
        if (f) {
          name = f.name;
          b64 = await new Promise((res, rej) => {
            const fr = new FileReader();
            fr.onload = () => res(fr.result.split(",")[1]);
            fr.onerror = rej; fr.readAsDataURL(f);
          });
        }
        await submit();
        closeModal(); toast("交接已保存", "ok"); render("handover");
      } catch (e) { toast(e.message, "err"); }
    } },
  ]);
}

// ---------------- 视图 7：回访统计 ----------------
async function viewCallback(el) {
  const role = state.user.role;
  const d = await api("GET", "/api/callback-stats?days=7");
  const cd = await api("GET", "/api/complaints?station_id=1");
  const done = cd.complaints.filter(c => c.status === "resolved");

  const maxN = Math.max(1, ...d.by_day.map(x => x.total));
  el.innerHTML = `
    <div class="grid grid-3">
      <div class="card" style="margin-bottom:16px">
        <h2>近 7 天回访满意率</h2>
        <div class="satisfaction-ring">
          ${ringSvg(d.satisfaction)}
          <div>
            <div style="font-size:24px;font-weight:650">${d.satisfaction}%</div>
            <div class="muted">共 ${d.total} 次回访 · 不满意 ${d.unsatisfied} 次</div>
            ${d.unsatisfied ? badge("不满意已生成独立事件", "badge-amber") : ""}
          </div>
        </div>
      </div>
      <div class="card" style="grid-column:span 2;margin-bottom:16px">
        <h2>每日回访构成 <span class="sub">蓝=满意 红=不满意</span></h2>
        <div class="bar-chart">
          ${d.by_day.length === 0 ? `<p class="muted">暂无回访</p>` : d.by_day.map(x => {
            const h = x.total / maxN * 120;
            const okH = (x.ok / x.total) * h;
            return `<div class="col">
              <span class="muted" style="font-size:11px">${x.total}</span>
              <div class="stack" style="height:${h}px">
                <div class="seg-ok" style="height:${okH}px"></div>
                <div class="seg-bad" style="height:${h - okH}px"></div>
              </div>
              <span class="d">${x.d.slice(5)}</span>
            </div>`;
          }).join("")}
        </div>
      </div>
    </div>
    <div class="card">
      <h2>待回访 / 已处置工单 <span class="sub">不满意回访进入「回访不满意」事件单独处置</span></h2>
      <table>
        <thead><tr><th>处置时间</th><th>楼栋/房号</th><th>住户</th><th>类型</th><th>内容</th><th></th></tr></thead>
        <tbody>${done.slice(0, 30).map(c => `<tr>
          <td>${c.resolved_at ? fmtTime(c.resolved_at) : "—"}</td>
          <td>${c.building_name} ${esc(c.room || "")}</td>
          <td>${esc(c.resident_name || "匿名")}</td>
          <td class="kind-${c.kind}">${KIND_LABEL[c.kind]}</td>
          <td style="max-width:300px">${esc(c.content)}</td>
          <td style="text-align:right">
            ${["service", "dispatcher", "manager"].includes(role)
              ? `<button class="btn btn-sm" data-cb="${c.id}">录入回访</button>` : ""}
          </td></tr>`).join("")}</tbody>
      </table>
    </div>`;
  $$("button[data-cb]", el).forEach(b => b.addEventListener("click", () =>
    openCallbackModal(Number(b.dataset.cb))));
}

function ringSvg(pct) {
  const R = 40, C = 2 * Math.PI * R;
  const col = pct >= 90 ? "#2a78d6" : pct >= 70 ? "#eda100" : "#d03b3b";
  return `<svg class="ring" viewBox="0 0 96 96">
    <circle cx="48" cy="48" r="${R}" fill="none" stroke="#eef2f6" stroke-width="10"/>
    <circle cx="48" cy="48" r="${R}" fill="none" stroke="${col}" stroke-width="10"
      stroke-linecap="round" stroke-dasharray="${C * pct / 100} ${C}"
      transform="rotate(-90 48 48)"/>
  </svg>`;
}

// ---------------- 弹窗 ----------------
function modal(title, bodyHtml, buttons) {
  closeModal();
  const mask = document.createElement("div");
  mask.className = "modal-mask";
  mask.id = "modal-mask";
  mask.innerHTML = `<div class="modal"><h3>${esc(title)}</h3>${bodyHtml}
    <div class="modal-actions" id="modal-btns"></div></div>`;
  document.body.appendChild(mask);
  const box = $("#modal-btns", mask);
  buttons.forEach(b => {
    const btn = document.createElement("button");
    btn.className = "btn " + (b.primary ? "btn-primary" : "");
    btn.textContent = b.text;
    btn.addEventListener("click", b.onClick);
    box.appendChild(btn);
  });
  mask.addEventListener("mousedown", e => { if (e.target === mask) closeModal(); });
}
function closeModal() { $("#modal-mask")?.remove(); }
function promptModal(title) {
  return new Promise(resolve => {
    modal(title, `<div class="field"><textarea id="pm-v" rows="3"
      placeholder="请输入原因（必填）"></textarea></div>`, [
      { text: "取消", onClick: () => { closeModal(); resolve(null); } },
      { text: "确认", primary: true, onClick: () => {
        const v = $("#pm-v").value.trim();
        if (!v) return toast("请填写原因", "err");
        closeModal(); resolve(v);
      } },
    ]);
  });
}

// ---------------- 时钟 ----------------
setInterval(() => {
  const el = $("#clock");
  if (el) el.textContent = new Date().toLocaleString("zh-CN", { hour12: false });
}, 1000);

const VIEWS = {
  overview: viewOverview, curves: viewCurves, trend: viewTrend,
  approval: viewApproval, complaints: viewComplaints,
  handover: viewHandover, callback: viewCallback,
};

// 已登录则直接进入
(async () => {
  try {
    const d = await fetch("/api/me", { credentials: "same-origin" }).then(r => r.json());
    if (d.user) { state.user = d.user; await bootstrap(); }
  } catch (e) { /* 未登录 */ }
})();
