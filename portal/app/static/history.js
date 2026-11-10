"use strict";
(() => {
  let days = 30, page = 1;
  const PER_PAGE = 50;

  function fillDays(daily, nDays) {
    // continuous day axis from the first day with data (or range start) to today
    if (!daily.length) return [];
    const byDay = Object.fromEntries(daily.map(d => [d.day, d]));
    const today = new Date(); today.setHours(0, 0, 0, 0);
    let start = new Date(daily[0].day + "T00:00:00");
    const rangeStart = new Date(today.getTime() - (nDays - 1) * 86400000);
    if (nDays <= 365 && rangeStart > start) start = rangeStart;
    const out = [];
    for (let d = new Date(start); d <= today; d.setDate(d.getDate() + 1)) {
      const key = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
      out.push(byDay[key] || { day: key, n: 0, ok: 0, failed: 0, skipped: 0, other: 0, bytes: 0, files: 0, max_duration: null });
    }
    return out;
  }

  function tile(label, value, sub) {
    return el("div", { class: "card tile" }, el("div", { class: "label", text: label }),
      el("div", { class: "value", text: value }), el("div", { class: "sub", text: sub || "" }));
  }

  async function loadSummary() {
    const d = await api("GET", "/api/history/summary?days=" + days);
    $("#indexing").hidden = !d.indexing;
    const s = d.summary;
    const counted = s.success + s.failed;
    const t = clear($("#tiles"));
    t.style.margin = "0 0 16px";
    t.append(
      tile("Runs", fmtNum(s.runs), fmtNum(s.skipped) + " skipped"),
      tile("Success rate", counted ? (s.success / counted * 100).toFixed(s.failed ? 1 : 0) + "%" : "—", fmtNum(s.failed) + " failed"),
      tile("Data copied", fmtBytes(s.bytes), "in range"),
      tile("Files copied", fmtNum(s.files), "in range"));

    const daily = fillDays(d.daily, days);
    barChart($("#ch-bytes"), {
      label: "Data transferred per day", bytes: true, yFormat: v => fmtBytes(v, 0),
      data: daily.map(x => ({ label: x.day, values: { b: x.bytes } })),
      keys: [{ key: "b", name: "copied", color: cssVar("--s1") }],
      xLabel: dd => fmtDay(dd.label), titleFor: dd => fmtDay(dd.label),
    });
    const outKeys = [
      { key: "ok", name: "Success", color: cssVar("--good") },
      { key: "failed", name: "Failed", color: cssVar("--critical") },
      { key: "other", name: "Incomplete", color: cssVar("--serious") },
      { key: "skipped", name: "Skipped", color: cssVar("--axis") },
    ];
    clear($("#lg-outcomes")).append(legend(outKeys.map(k => ({ name: k.name, color: k.color }))));
    barChart($("#ch-outcomes"), {
      label: "Run outcomes per day",
      data: daily.map(x => ({ label: x.day, values: { ok: x.ok, failed: x.failed, other: x.other, skipped: x.skipped } })),
      keys: outKeys, yFormat: v => fmtNum(Math.round(v)),
      xLabel: dd => fmtDay(dd.label), titleFor: dd => fmtDay(dd.label),
    });
    barChart($("#ch-duration"), {
      label: "Longest successful run per day",
      data: daily.map(x => ({ label: x.day, values: { d: x.max_duration || 0 } })),
      keys: [{ key: "d", name: "longest run", color: cssVar("--s1") }],
      yFormat: v => fmtDuration(v), xLabel: dd => fmtDay(dd.label), titleFor: dd => fmtDay(dd.label),
    });
  }

  async function loadGrowth() {
    const d = await api("GET", "/api/history/growth?days=" + Math.max(days, 7));
    const names = Object.keys(d.series).sort();
    const series = names.map((src, i) => ({
      name: src, color: cssVar(SERIES[i % SERIES.length]),
      points: d.series[src].map(([day, size]) => [new Date(day + "T00:00:00").getTime() / 1000, size]),
    }));
    clear($("#lg-growth")).append(series.length > 1 ? legend(series.map(s => ({ name: s.name, color: s.color })), "line") : "");
    lineChart($("#ch-growth"), { series, label: "Source size over time", yFormat: v => fmtBytes(v), emptyText: "No size data in this range" });
  }

  async function toggleDetail(tr, run) {
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("detail")) { next.remove(); tr.setAttribute("aria-expanded", "false"); return; }
    tr.setAttribute("aria-expanded", "true");
    const td = el("td", { colspan: 7, text: "Loading…" });
    tr.after(el("tr", { class: "detail" }, td));
    try {
      const r = await api("GET", "/api/history/run/" + encodeURIComponent(run.run_id));
      clear(td);
      td.append(el("div", { class: "small muted mono", text: "Run " + r.run_id + (r.backfilled ? " (imported from log)" : "") }));
      if (r.reason) td.append(el("div", { class: "msg " + (r.status === "failed" ? "error" : "info"), text: r.reason }));
      if (!r.jobs.length) { td.append(el("p", { class: "muted small", text: "No jobs ran." })); return; }
      const tb = el("tbody");
      for (const j of r.jobs) {
        const st = j.stats || j;
        tb.append(el("tr", {},
          el("td", {}, el("div", { text: j.name }), el("div", { class: "small muted mono", text: (j.source || "") + " → " + (j.dest || "") })),
          el("td", {}, statusPill(j.status)),
          el("td", { class: "num", text: fmtDuration(j.end_ts && j.start_ts ? j.end_ts - j.start_ts : null) }),
          el("td", { class: "num", text: fmtNum(st.transferred_files) }),
          el("td", { class: "num", text: fmtBytes(st.transferred_bytes) }),
          el("td", { class: "num", text: st.bytes_per_sec ? fmtBytes(st.bytes_per_sec) + "/s" : "—" }),
          el("td", { class: "num", text: fmtNum(st.files) }),
          el("td", { class: "num", text: fmtBytes(st.total_size) }),
          el("td", { class: "small", text: j.reason || "" })));
      }
      td.append(el("div", { class: "table-wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, ["Job", "Status", "Duration", "Files copied", "Copied", "Speed", "Files in source", "Source size", ""].map((h, i) => el("th", { class: i >= 2 && i <= 7 ? "num" : null, text: h })))),
        tb)));
    } catch (e) { td.textContent = e.message; }
  }

  async function loadRuns() {
    const q = new URLSearchParams({ days, page, per_page: PER_PAGE, status: $("#status-filter").value, only_changes: $("#only-changes").checked ? "1" : "0" });
    const d = await api("GET", "/api/history/runs?" + q);
    const tb = clear($("#runs"));
    for (const r of d.runs) {
      const tr = el("tr", { class: "clickable", tabindex: 0, "aria-expanded": "false" },
        el("td", { text: fmtTime(r.start) }),
        el("td", {}, statusPill(r.status)),
        el("td", { class: "ink2", text: r.trigger || "cron" }),
        el("td", { class: "num", text: fmtDuration(r.duration) }),
        el("td", { class: "num", text: fmtNum(r.transferred_files) }),
        el("td", { class: "num", text: fmtBytes(r.transferred_bytes) }),
        el("td", { class: "small ink2", text: r.reason || "" }));
      tr.addEventListener("click", () => toggleDetail(tr, r));
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleDetail(tr, r); } });
      tb.append(tr);
    }
    if (!d.runs.length) tb.append(el("tr", {}, el("td", { colspan: 7, class: "muted", text: "No runs match these filters." })));
    const pages = Math.max(1, Math.ceil(d.total / PER_PAGE));
    const pager = clear($("#pager"));
    pager.style.marginTop = "12px";
    const prev = el("button", { text: "← Newer", disabled: page <= 1 });
    const next = el("button", { text: "Older →", disabled: page >= pages });
    prev.addEventListener("click", () => { page--; loadRuns(); });
    next.addEventListener("click", () => { page++; loadRuns(); });
    pager.append(prev, el("span", { class: "small ink2", text: `Page ${page} of ${pages} · ${fmtNum(d.total)} runs` }), next);
  }

  function loadAll() {
    page = 1;
    loadSummary().catch(e => toast(e.message, "error"));
    loadGrowth().catch(e => toast(e.message, "error"));
    loadRuns().catch(e => toast(e.message, "error"));
  }

  document.querySelectorAll("#range button").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll("#range button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    days = Number(b.dataset.days);
    loadAll();
  }));
  $("#status-filter").addEventListener("change", () => { page = 1; loadRuns(); });
  $("#only-changes").addEventListener("change", () => { page = 1; loadRuns(); });
  loadAll();
})();
