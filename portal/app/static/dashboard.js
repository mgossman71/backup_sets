"use strict";
(() => {
  let state = null;
  let pendingSince = null;

  function bigStatus(s) {
    const box = clear($("#status-main"));
    const f = s.flags;
    let icon, cls, title, sub;
    if (f.running) {
      icon = "↻"; cls = "info"; title = f.running.by === "cleanup" ? "Backup Folder Cleanup in progress" : "Backup running";
      sub = "Started " + relTime(f.running.since);
    } else if (f.failed) {
      icon = "✕"; cls = "critical"; title = "Last backup failed";
      sub = (s.last_failure && s.last_failure.reason) || "See the log for details";
    } else if (f.stopped) {
      icon = "⏸"; cls = "warning"; title = "Scheduled backups paused";
      sub = "Paused " + relTime(f.stopped.since) + ". Cron runs will exit without copying anything.";
    } else {
      icon = "✓"; cls = "good"; title = "Backups healthy";
      sub = s.last_success ? "Last successful run " + relTime(s.last_success.end || s.last_success.start) : "No successful runs recorded yet";
    }
    box.append(el("div", { class: "icon " + cls, "aria-hidden": "true", text: icon }),
               el("div", {}, el("div", { class: "title", text: title }), el("div", { class: "ink2", text: sub })));

    const flags = clear($("#status-flags"));
    flags.style.margin = "12px 0";
    for (const sch of s.schedules) {
      flags.append(el("span", { class: "small ink2" },
        "Next scheduled run: ", el("b", { text: sch.next ? fmtTime(sch.next) : "?" }),
        sch.next ? " (" + relTime(sch.next) + ")" : "", el("span", { class: "muted mono", text: "  " + sch.expr })));
    }
    if (!s.schedules.length) flags.append(el("span", { class: "pill warning", text: "Automatic backups are off: no schedule is set" }));
    flags.append(el("a", { class: "small", href: "/selections#schedule", text: s.schedules.length ? "Change schedule" : "Set a schedule" }));

    if (f.stale_running) {
      flags.append(el("span", { class: "pill warning",
        text: "A previous run didn't finish cleanly (pid " + (f.stale_running.pid || "?") + "). The next backup clears its leftover running flag." }));
    }

    const act = clear($("#activity"));
    if (f.running && s.activity) {
      const a = s.activity;
      act.style.marginBottom = "12px";
      if (a.current_job) act.append(el("div", {}, "Now copying ", el("b", { text: a.current_job })));
      if (a.completed_jobs && a.completed_jobs.length) act.append(el("div", { text: "Finished: " + a.completed_jobs.join(", ") }));
      if (a.last_file) act.append(el("div", { class: "mono muted", text: a.last_file }));
    }

    const pend = s.pending_actions;
    const runBtn = $("#btn-run"), stopBtn = $("#btn-stop"), clrBtn = $("#btn-clear");
    runBtn.disabled = !!f.running || !!f.stopped || pend.includes("run");
    runBtn.title = f.stopped ? "Resume scheduled backups first. backup.sh exits while paused." : f.running ? "A backup is already running" : "";
    stopBtn.textContent = f.stopped ? "Resume scheduled backups" : "Pause scheduled backups";
    stopBtn.dataset.action = f.stopped ? "stop_off" : "stop_on";
    stopBtn.disabled = pend.includes("stop_on") || pend.includes("stop_off");
    clrBtn.hidden = !f.failed;
    clrBtn.disabled = pend.includes("clear_fail");

    const p = $("#pending");
    if (pend.length) {
      pendingSince = pendingSince || Date.now();
      p.hidden = false;
      p.textContent = Date.now() - pendingSince > 15000
        ? "Requests are still waiting (" + pend.join(", ") + "). Is backup-portal-actions.path running on the host?"
        : "Sending to the server: " + pend.join(", ") + "…";
    } else { pendingSince = null; p.hidden = true; }

    $("#indexing").hidden = !s.indexing;
  }

  function lastRun(run) {
    const box = clear($("#last-run"));
    if (!run) { box.append(el("p", { class: "muted", text: "No runs recorded yet." })); return; }
    box.append(el("div", { class: "row" }, statusPill(run.status),
      el("span", { text: fmtTime(run.start) }), el("span", { class: "muted", text: "· " + fmtDuration(run.duration) + " · " + (run.trigger || "cron") })));
    if (run.reason) box.append(el("div", { class: "msg " + (run.status === "failed" ? "error" : "info"), text: run.reason }));
    if (!run.jobs || !run.jobs.length) return;
    const tb = el("tbody");
    for (const j of run.jobs) {
      tb.append(el("tr", {},
        el("td", {}, el("div", { text: j.name }), el("div", { class: "small muted mono", text: j.dest || "" })),
        el("td", {}, statusPill(j.status)),
        el("td", { class: "num", text: fmtDuration(j.end_ts && j.start_ts ? j.end_ts - j.start_ts : null) }),
        el("td", { class: "num", text: fmtNum(j.transferred_files) }),
        el("td", { class: "num", text: fmtBytes(j.transferred_bytes) }),
        el("td", { class: "num", text: fmtBytes(j.total_size) })));
    }
    box.append(el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", { text: "Job" }), el("th", { text: "Status" }), el("th", { class: "num", text: "Duration" }),
        el("th", { class: "num", text: "Files copied" }), el("th", { class: "num", text: "Copied" }), el("th", { class: "num", text: "Source size" }))),
      tb)));
  }

  function tiles(s) {
    const t = clear($("#tiles"));
    t.style.margin = "16px 0";
    const sm = s.summary;
    const counted = sm.success + sm.failed;
    const rate = counted ? (sm.success / counted * 100) : null;
    const tile = (label, value, sub) => el("div", { class: "card tile" }, el("div", { class: "label", text: label }),
      el("div", { class: "value", text: value }), el("div", { class: "sub", text: sub || "" }));
    t.append(
      tile("Data copied", fmtBytes(sm.bytes), "last 30 days"),
      tile("Files copied", fmtNum(sm.files), "last 30 days"),
      tile("Successful runs", rate === null ? "—" : rate.toFixed(rate === 100 ? 0 : 1) + "%", fmtNum(sm.success) + " of " + fmtNum(counted) + " (excl. skipped)"),
      tile("Last failure", s.last_failure ? relTime(s.last_failure.start) : "None", s.last_failure ? fmtTime(s.last_failure.start) : "on record"));
  }

  async function loadStatus() {
    state = await api("GET", "/api/status");
    bigStatus(state);
    lastRun(state.last_run);
    tiles(state);
  }

  async function loadFs() {
    const d = await api("GET", "/api/filesystems");
    const box = clear($("#fs-tiles"));
    const list = d.filesystems.filter(f => f.roles.length || f.required);
    for (const f of list) {
      const card = el("div", { class: "card fs-tile" });
      if (!f.ok) {
        card.append(el("div", { class: "mount", text: f.mount }), el("div", { class: "lvl-label critical", text: "✕ " + (f.error || "unavailable") }));
      } else {
        card.append(
          el("div", { class: "head" }, el("span", { class: "mount", title: f.mount, text: f.mount }), el("span", { class: "pct", text: f.pct.toFixed(0) + "%" })),
          meter(f.pct, f.level),
          el("div", { class: "foot" },
            el("span", { class: "lvl-label " + f.level, text: levelIcon(f.level) + levelLabel(f.level) }),
            el("span", { text: fmtBytes(f.avail) + " free of " + fmtBytes(f.size) })),
          el("div", { class: "small muted", text: f.days_to_full ? "Full in ~" + fmtDays(f.days_to_full) : "" }));
      }
      box.append(card);
    }
    if (!list.length) box.append(el("p", { class: "muted", text: "No backup filesystems detected." }));
  }

  async function loadLog() {
    const d = await api("GET", "/api/log?lines=300");
    const box = $("#log");
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    clear(box);
    for (const line of d.lines) {
      let cls = null;
      if (/\bERROR\b/.test(line)) cls = "err";
      else if (/Successfully completed|completed successfully|✓/.test(line)) cls = "ok";
      else if (/Starting backup:|Backup script started|Portal action/.test(line)) cls = "hdr";
      box.append(el("div", { class: cls, text: line }));
    }
    if ($("#log-follow").checked || atBottom) box.scrollTop = box.scrollHeight;
  }

  async function act(name, label) {
    try {
      await api("POST", "/api/action/" + name);
      toast(label);
      setTimeout(() => statusPoll.now(), 1500);
    } catch (e) { toast(e.message, "error"); }
  }
  $("#btn-run").addEventListener("click", () => act("run", "Backup requested"));
  $("#btn-stop").addEventListener("click", (e) => {
    const a = e.currentTarget.dataset.action;
    act(a, a === "stop_on" ? "Pausing scheduled backups" : "Resuming scheduled backups");
  });
  $("#btn-clear").addEventListener("click", () => act("clear_fail", "Clearing failed status"));

  const fast = () => (state && (state.flags.running || state.pending_actions.length)) ? 4000 : 20000;
  const statusPoll = poll(loadStatus, fast);
  poll(loadLog, fast);
  poll(loadFs, 60000);
})();
