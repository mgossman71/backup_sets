/* Shared helpers + small dependency-free SVG charts for the Backup Sets portal. */
"use strict";

const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || "";

async function api(method, url, body) {
  const opts = { method, headers: { "X-CSRF-Token": CSRF }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (res.status === 401) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
    throw new Error("not logged in");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    const err = new Error((data && (data.error || (data.errors || []).join("; "))) || res.statusText);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

/* ---------- DOM ---------- */
function el(tag, attrs, ...children) {
  const svg = ["svg", "g", "rect", "line", "path", "text", "circle", "title"].includes(tag);
  const node = svg ? document.createElementNS("http://www.w3.org/2000/svg", tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.setAttribute("class", v);
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
const $ = (sel, root = document) => root.querySelector(sel);
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

let toastTimer;
function toast(msg, kind) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = kind || "";
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, kind === "error" ? 6000 : 3000);
}

/* ---------- formatting ---------- */
function fmtBytes(n, digits = 1) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  const u = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let i = 0, v = Math.abs(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (n < 0 ? "-" : "") + (i === 0 ? v.toFixed(0) : v.toFixed(v >= 100 ? 0 : digits)) + " " + u[i];
}
function fmtNum(n) { return n === null || n === undefined ? "—" : Number(n).toLocaleString(); }
function fmtDuration(s) {
  if (s === null || s === undefined || isNaN(s)) return "—";
  s = Math.round(s);
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60), sec = s % 60;
  if (m < 60) return m + "m " + String(sec).padStart(2, "0") + "s";
  const h = Math.floor(m / 60);
  if (h < 48) return h + "h " + String(m % 60).padStart(2, "0") + "m";
  return Math.floor(h / 24) + "d " + (h % 24) + "h";
}
function toDate(v) { return typeof v === "number" ? new Date(v * 1000) : new Date(v); }
function fmtTime(v) {
  if (!v) return "—";
  const d = toDate(v);
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function fmtDay(s) {
  const d = new Date(s + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
function relTime(v) {
  if (!v) return "";
  const diff = (toDate(v).getTime() - Date.now()) / 1000;
  const a = Math.abs(diff);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (a < 60) return rtf.format(Math.round(diff), "second");
  if (a < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (a < 86400 * 2) return rtf.format(Math.round(diff / 3600), "hour");
  return rtf.format(Math.round(diff / 86400), "day");
}
function fmtDays(d) {
  if (d === null || d === undefined) return "—";
  if (d > 3650) return "10+ years";
  if (d > 730) return (d / 365).toFixed(1) + " years";
  if (d > 90) return Math.round(d / 30) + " months";
  return Math.max(0, Math.round(d)) + " days";
}

const STATUS = {
  success: { cls: "good", label: "Success" },
  failed: { cls: "critical", label: "Failed" },
  skipped: { cls: "", label: "Skipped" },
  stopped: { cls: "warning", label: "Paused mid-run" },
  incomplete: { cls: "serious", label: "Incomplete" },
  running: { cls: "running", label: "Running" },
};
function statusPill(s) {
  const m = STATUS[s] || { cls: "", label: s || "—" };
  return el("span", { class: "pill " + m.cls, text: m.label });
}
function levelLabel(level) {
  return { good: "OK", warning: "Getting full", critical: "Nearly full", unknown: "Unknown" }[level] || level;
}
function levelIcon(level) { return { good: "✓ ", warning: "! ", critical: "✕ " }[level] || "? "; }
function meter(pct, level) {
  const m = el("div", { class: "meter " + (level || ""), role: "meter", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": pct });
  const s = el("span");
  s.style.width = Math.min(100, Math.max(0, pct || 0)) + "%";
  m.append(s);
  return m;
}
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
const SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];

/* ---------- tooltip ---------- */
const tip = (() => {
  let node;
  function ensure() {
    if (!node) { node = el("div", { class: "tooltip", role: "tooltip" }); node.hidden = true; document.body.append(node); }
    return node;
  }
  return {
    show(evt, title, rows) {
      const n = clear(ensure());
      n.append(el("div", { class: "tt-title", text: title }));
      for (const r of rows) {
        const key = el("i");
        if (r.color) key.style.background = r.color;
        n.append(el("div", { class: "tt-row" }, r.color ? key : null, el("b", { text: r.value }), r.name ? el("span", { class: "name", text: r.name }) : null));
      }
      n.hidden = false;
      const pad = 14, w = n.offsetWidth, h = n.offsetHeight;
      let x = evt.clientX + pad, y = evt.clientY + pad;
      if (x + w > innerWidth - 8) x = evt.clientX - w - pad;
      if (y + h > innerHeight - 8) y = evt.clientY - h - pad;
      n.style.left = x + "px"; n.style.top = y + "px";
    },
    hide() { if (node) node.hidden = true; },
  };
})();

/* ---------- charts ---------- */
function niceMax(v) {
  if (!v || v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}
function niceBytesMax(v) {
  if (!v || v <= 0) return 1024;
  const u = Math.pow(1024, Math.floor(Math.log(v) / Math.log(1024)));
  return niceMax(v / u) * u;
}

/* Responsive redraw: calls draw(width) now and whenever the container resizes. */
function responsive(container, draw) {
  let last = 0;
  const ro = new ResizeObserver(() => {
    const w = Math.floor(container.clientWidth);
    if (w && w !== last) { last = w; draw(w); }
  });
  ro.observe(container);
  container._ro && container._ro.disconnect();
  container._ro = ro;
}

/*
 * Bar chart (single series or stacked).
 * opts: { data: [{label, values: {key: n}}], keys: [{key, name, color}], height,
 *         yFormat, bytes (bool), titleFor(d) }
 */
function barChart(container, opts) {
  clear(container);
  container.classList.add("chart");
  const H = opts.height || 200, M = { t: 8, r: 8, b: 24, l: 58 };
  const data = opts.data || [];
  const keys = opts.keys;
  const fmt = opts.yFormat || (v => fmtNum(v));
  responsive(container, (W) => {
    clear(container);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.label || "bar chart" });
    container.append(svg);
    if (!data.length) {
      svg.append(el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "empty", text: "No data for this range" }));
      return;
    }
    const totals = data.map(d => keys.reduce((s, k) => s + (d.values[k.key] || 0), 0));
    const max = opts.bytes ? niceBytesMax(Math.max(...totals)) : niceMax(Math.max(...totals));
    const iw = W - M.l - M.r, ih = H - M.t - M.b;
    const y = v => M.t + ih - (v / max) * ih;
    for (let i = 0; i <= 4; i++) {
      const v = (max / 4) * i, yy = y(v);
      svg.append(el("line", { x1: M.l, x2: W - M.r, y1: yy, y2: yy, class: i === 0 ? "baseline" : "gridline" }));
      svg.append(el("text", { x: M.l - 6, y: yy + 4, "text-anchor": "end", class: "tick", text: fmt(v) }));
    }
    const slot = iw / data.length;
    const bw = Math.max(1, Math.min(28, slot - 2));
    const labelEvery = Math.ceil(data.length / Math.max(1, Math.floor(iw / 64)));
    data.forEach((d, i) => {
      const x = M.l + i * slot + (slot - bw) / 2;
      let acc = 0;
      const g = el("g", { tabindex: 0 });
      const segs = keys.filter(k => d.values[k.key] > 0);
      segs.forEach((k, si) => {
        const v = d.values[k.key];
        const y0 = y(acc), y1 = y(acc + v);
        const top = si === segs.length - 1;
        const hgt = Math.max(0, y0 - y1 - (si > 0 ? 2 : 0));  // 2px surface gap between segments
        const r = top ? Math.min(4, bw / 2, hgt) : 0;
        const yTop = y1;
        const path = r > 0
          ? `M${x},${yTop + hgt} V${yTop + r} Q${x},${yTop} ${x + r},${yTop} H${x + bw - r} Q${x + bw},${yTop} ${x + bw},${yTop + r} V${yTop + hgt} Z`
          : `M${x},${yTop + hgt} V${yTop} H${x + bw} V${yTop + hgt} Z`;
        const p = el("path", { d: path, class: "bar" });
        p.style.fill = k.color;
        g.append(p);
        acc += v;
      });
      // hit target: full column
      const hit = el("rect", { x: M.l + i * slot, y: M.t, width: slot, height: ih, fill: "transparent" });
      g.append(hit);
      const show = (evt) => {
        g.querySelectorAll(".bar").forEach(b => b.classList.add("hover"));
        const rows = keys.map(k => ({ color: keys.length > 1 ? k.color : null, value: fmt(d.values[k.key] || 0), name: k.name }));
        tip.show(evt, opts.titleFor ? opts.titleFor(d) : d.label, rows);
      };
      const hide = () => { g.querySelectorAll(".bar").forEach(b => b.classList.remove("hover")); tip.hide(); };
      g.addEventListener("pointermove", show);
      g.addEventListener("pointerleave", hide);
      g.addEventListener("focus", () => { const r = hit.getBoundingClientRect(); show({ clientX: r.x + r.width / 2, clientY: r.y + 20 }); });
      g.addEventListener("blur", hide);
      svg.append(g);
      if (i % labelEvery === 0) {
        svg.append(el("text", { x: M.l + i * slot + slot / 2, y: H - 6, "text-anchor": "middle", class: "tick", text: opts.xLabel ? opts.xLabel(d) : d.label }));
      }
    });
  });
}

/*
 * Line chart with crosshair.
 * opts: { series: [{name, color, points: [[t(sec), v], ...]}], height, yFormat, yMax, bytes }
 */
function lineChart(container, opts) {
  clear(container);
  container.classList.add("chart");
  const H = opts.height || 220, M = { t: 10, r: 12, b: 24, l: 62 };
  const series = (opts.series || []).filter(s => s.points.length);
  const fmt = opts.yFormat || (v => fmtNum(v));
  responsive(container, (W) => {
    clear(container);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.label || "line chart" });
    container.append(svg);
    if (!series.length) {
      svg.append(el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "empty", text: opts.emptyText || "No data yet" }));
      return;
    }
    const ts = series.flatMap(s => s.points.map(p => p[0]));
    let t0 = Math.min(...ts), t1 = Math.max(...ts);
    if (t1 === t0) { t0 -= 3600; t1 += 3600; }
    const vals = series.flatMap(s => s.points.map(p => p[1]));
    let vmin = opts.yMin !== undefined ? opts.yMin : Math.min(...vals);
    let vmax = opts.yMax !== undefined ? opts.yMax : Math.max(...vals);
    if (opts.yMin === undefined) {  // pad the range a little so lines don't sit on the frame
      const pad = (vmax - vmin) * 0.1 || Math.abs(vmax) * 0.05 || 1;
      vmin = Math.max(0, vmin - pad); vmax = vmax + pad;
    }
    const iw = W - M.l - M.r, ih = H - M.t - M.b;
    const x = t => M.l + ((t - t0) / (t1 - t0)) * iw;
    const y = v => M.t + ih - ((v - vmin) / (vmax - vmin || 1)) * ih;
    for (let i = 0; i <= 4; i++) {
      const v = vmin + ((vmax - vmin) / 4) * i, yy = y(v);
      svg.append(el("line", { x1: M.l, x2: W - M.r, y1: yy, y2: yy, class: i === 0 ? "baseline" : "gridline" }));
      svg.append(el("text", { x: M.l - 6, y: yy + 4, "text-anchor": "end", class: "tick", text: fmt(v) }));
    }
    const nTicks = Math.max(2, Math.min(8, Math.floor(iw / 90)));
    const spanDays = (t1 - t0) / 86400;
    for (let i = 0; i <= nTicks; i++) {
      const t = t0 + ((t1 - t0) / nTicks) * i;
      const d = new Date(t * 1000);
      const label = spanDays > 2 ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
                                 : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
      svg.append(el("text", { x: x(t), y: H - 6, "text-anchor": i === 0 ? "start" : i === nTicks ? "end" : "middle", class: "tick", text: label }));
    }
    for (const s of series) {
      const d = s.points.map((p, i) => (i ? "L" : "M") + x(p[0]).toFixed(1) + "," + y(p[1]).toFixed(1)).join("");
      const path = el("path", { d, fill: "none", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
      path.style.stroke = s.color;
      svg.append(path);
      if (s.points.length === 1) {
        const c = el("circle", { cx: x(s.points[0][0]), cy: y(s.points[0][1]), r: 4 });
        c.style.fill = s.color;
        svg.append(c);
      }
    }
    // crosshair layer
    const cross = el("line", { y1: M.t, y2: M.t + ih, class: "crosshair" });
    cross.style.display = "none";
    svg.append(cross);
    const dots = series.map(s => { const c = el("circle", { r: 4, "stroke-width": 2 }); c.style.fill = s.color; c.style.stroke = cssVar("--surface"); c.style.display = "none"; svg.append(c); return c; });
    const hit = el("rect", { x: M.l, y: M.t, width: iw, height: ih, fill: "transparent" });
    svg.append(hit);
    hit.addEventListener("pointermove", (evt) => {
      const r = svg.getBoundingClientRect();
      const t = t0 + ((evt.clientX - r.left) * (W / r.width) - M.l) / iw * (t1 - t0);
      let nearestT = null;
      const rows = [];
      series.forEach((s, i) => {
        let best = s.points[0];
        for (const p of s.points) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
        if (nearestT === null || Math.abs(best[0] - t) < Math.abs(nearestT - t)) nearestT = best[0];
        dots[i].setAttribute("cx", x(best[0])); dots[i].setAttribute("cy", y(best[1])); dots[i].style.display = "";
        rows.push({ color: s.color, value: fmt(best[1]), name: s.name, v: best[1] });
      });
      rows.sort((a, b) => b.v - a.v);
      cross.setAttribute("x1", x(nearestT)); cross.setAttribute("x2", x(nearestT)); cross.style.display = "";
      tip.show(evt, fmtTime(nearestT), rows);
    });
    hit.addEventListener("pointerleave", () => { cross.style.display = "none"; dots.forEach(d => d.style.display = "none"); tip.hide(); });
  });
}

function legend(items, kind) {
  return el("div", { class: "legend" }, items.map(it => {
    const i = el("i", { class: kind === "line" ? "line" : "" });
    i.style.background = it.color;
    return el("span", {}, i, it.name);
  }));
}

/* Simple poller that pauses while the tab is hidden. */
function poll(fn, intervalFn) {
  let timer;
  async function tick() {
    clearTimeout(timer);
    if (!document.hidden) { try { await fn(); } catch (e) { console.warn(e); } }
    timer = setTimeout(tick, typeof intervalFn === "function" ? intervalFn() : intervalFn);
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  tick();
  return { now: tick };
}
