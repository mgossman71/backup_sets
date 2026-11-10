"use strict";
(() => {
  let days = 7;
  let fsList = [];

  const roleText = (f) => [
    f.roles.includes("destination") ? "Backup destination" : null,
    f.roles.includes("source") ? "Backup source" : null,
    f.required ? "Required mount" : null,
  ].filter(Boolean).join(" · ");

  async function loadFs() {
    const d = await api("GET", "/api/filesystems");
    fsList = d.filesystems;
    $("#updated").textContent = "Updated " + new Date(d.server_time * 1000).toLocaleTimeString();
    const tb = clear($("#fs-rows"));
    for (const f of fsList) {
      if (!f.ok) {
        tb.append(el("tr", {}, el("td", { class: "path", text: f.mount }), el("td", { colspan: 8 },
          el("span", { class: "lvl-label critical", text: "✕ " + (f.error || "unavailable") }))));
        continue;
      }
      const m = meter(f.pct, f.level);
      m.style.minWidth = "120px";
      tb.append(el("tr", {},
        el("td", {}, el("div", { class: "path mono", text: f.mount }), el("div", { class: "small muted", text: f.device + " · " + f.fstype })),
        el("td", {}, el("div", { class: "row" }, m, el("b", { class: "num", text: f.pct.toFixed(1) + "%" }))),
        el("td", { class: "num", text: fmtBytes(f.size) }),
        el("td", { class: "num", text: fmtBytes(f.used) }),
        el("td", { class: "num", text: fmtBytes(f.avail) }),
        el("td", {}, el("span", { class: "lvl-label " + f.level, text: levelIcon(f.level) + levelLabel(f.level) })),
        el("td", { class: "num", title: f.growth_basis ? "Based on " + f.growth_basis : "", text: f.growth_per_day === null || f.growth_per_day === undefined ? "—" : (f.growth_per_day >= 0 ? "+" : "") + fmtBytes(f.growth_per_day) + "/day" }),
        el("td", { class: "num", title: f.growth_basis ? "Based on " + f.growth_basis : "", text: f.days_to_full ? "~" + fmtDays(f.days_to_full) : "—" }),
        el("td", { class: "small ink2", text: roleText(f) })));
    }

    const alerts = clear($("#alerts"));
    for (const f of fsList.filter(x => x.ok && x.level !== "good" && (x.roles.length || x.required))) {
      const soon = f.days_to_full && f.days_to_full < 60 ? " At the current rate it fills in about " + fmtDays(f.days_to_full) + "." : "";
      alerts.append(el("div", { class: "msg " + (f.level === "critical" ? "error" : "warn") },
        el("strong", { text: (f.level === "critical" ? "✕ " : "! ") + f.mount + " is " + f.pct.toFixed(0) + "% full" }),
        fmtBytes(f.avail) + " free." + soon + (f.backups.length ? " Backed up here: " + f.backups.join(", ") + "." : "")));
    }
  }

  async function loadTrend() {
    const d = await api("GET", "/api/filesystems/trend?days=" + days);
    // stable colors: by mount order in the table, highlighted (backup-related) filesystems first
    const order = fsList.filter(f => f.roles.length || f.required).map(f => f.mount)
      .concat(fsList.filter(f => !(f.roles.length || f.required)).map(f => f.mount));
    const mounts = order.filter(m => d.series[m]).slice(0, 8);
    const series = mounts.map((m, i) => ({
      name: m, color: cssVar(SERIES[i]),
      points: d.series[m].map(([t, used, size]) => [t, size ? used / size * 100 : 0]),
    }));
    clear($("#lg-trend")).append(series.length > 1 ? legend(series.map(s => ({ name: s.name, color: s.color })), "line") : "");
    lineChart($("#ch-trend"), {
      series, height: 260, label: "Percent used over time", yFormat: v => v.toFixed(0) + "%",
      emptyText: "No snapshots yet. The portal records usage once an hour.",
    });
  }

  document.querySelectorAll("#range button").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll("#range button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    days = Number(b.dataset.days);
    loadTrend().catch(e => toast(e.message, "error"));
  }));
  poll(async () => { await loadFs(); await loadTrend(); }, 60000);
})();
