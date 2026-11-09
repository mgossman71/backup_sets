"use strict";
/* Schedule card (Backup selections page): one schedule that runs all backup sets.
   Saving sends a request; the host rewrites only its own block in root's crontab. */
(() => {
  const DAYS = [[1, "Mon"], [2, "Tue"], [3, "Wed"], [4, "Thu"], [5, "Fri"], [6, "Sat"], [0, "Sun"]];
  let current = null, previewTimer = null, pollTimer = null, savedKey = null, lastExpr = null;

  // weekday checkboxes
  const daysBox = $("#sch-days");
  for (const [v, name] of DAYS) {
    const cb = el("input", { type: "checkbox", value: String(v), "aria-label": name });
    daysBox.append(el("label", { class: "sch-day" }, cb, name));
  }

  function formValues() {
    return {
      enabled: $("#sch-enabled").checked,
      kind: $("#sch-kind").value,
      time: $("#sch-time").value,
      days: [...daysBox.querySelectorAll("input:checked")].map((c) => Number(c.value)),
      every: Number($("#sch-every").value),
      minute: $("#sch-minute").value.trim() === "" ? 0 : Number($("#sch-minute").value),
      expr: $("#sch-expr").value,
    };
  }
  const keyOf = (v) => JSON.stringify([v.enabled, lastExpr]);

  function showKind() {
    const k = $("#sch-kind").value;
    $("#sch-time-wrap").hidden = !(k === "daily" || k === "weekdays");
    daysBox.hidden = k !== "weekdays";
    $("#sch-every-wrap").hidden = k !== "hours";
    $("#sch-expr-wrap").hidden = k !== "advanced";
    $("#sch-form").classList.toggle("off", !$("#sch-enabled").checked);
  }

  function fill(form, enabled, expr) {
    $("#sch-enabled").checked = enabled;
    $("#sch-kind").value = form.kind;
    if (form.time) $("#sch-time").value = form.time;
    daysBox.querySelectorAll("input").forEach((c) => { c.checked = (form.days || []).includes(Number(c.value)); });
    if (form.every) $("#sch-every").value = String(form.every);
    $("#sch-minute").value = String(form.minute || 0);
    $("#sch-expr").value = expr || form.expr || "";
    showKind();
  }

  const localTime = (t) => new Date(t * 1000).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
  const serverTime = (t) => new Date(t * 1000).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit", timeZone: "UTC" });

  function renderNotes() {
    const box = clear($("#sch-notes"));
    if (!current) return;
    if (current.broken) {
      box.append(el("div", { class: "msg error", text: "Root's crontab has a damaged portal schedule block (a BEGIN line without its END line). Fix it with crontab -e before saving here." }));
    }
    if (!current.enabled) {
      box.append(el("div", { class: "msg warn" }, el("strong", { text: "! Automatic backups are off" }),
        current.managed ? "The schedule is saved but turned off." : "No backup schedule is set in root's crontab, so backups only run when you click Run backup now."));
    }
    if (current.unmanaged.length) {
      box.append(el("div", { class: "msg info" }, el("strong", { text: "Existing cron line(s) found" }),
        "Saving here comments out these lines so backups don't run twice:",
        el("ul", {}, current.unmanaged.map((l) => el("li", { class: "mono", text: l })))));
    }
    $("#sch-tz").textContent = "server time (" + (current.timezone || "UTC") + ")";
  }

  async function load() {
    try {
      current = await api("GET", "/api/schedule");
    } catch (e) {
      clear($("#sch-notes")).append(el("div", { class: "msg error", text: "Could not read the schedule: " + e.message }));
      return;
    }
    fill(current.form, current.enabled, current.expr);
    lastExpr = current.expr;
    savedKey = current.managed ? JSON.stringify([current.enabled, current.expr]) : null;
    renderNotes();
    await preview();
  }

  async function preview() {
    const box = clear($("#sch-preview"));
    const v = formValues();
    let r;
    try {
      r = await api("POST", "/api/schedule/preview", v);
    } catch (e) {
      lastExpr = null;
      const errs = (e.data && e.data.errors) || [e.message];
      box.append(el("div", { class: "msg error", text: errs.join(" ") }));
      $("#sch-save").disabled = true;
      return;
    }
    lastExpr = r.expr;
    if (v.enabled) {
      box.append(el("div", {}, el("b", { text: r.form.text }), " server time ",
        el("span", { class: "mono muted", text: "(" + r.expr + ")" })),
        el("div", { class: "next", text: "Next runs: " + r.next_runs.slice(0, 3).map((t) => serverTime(t) + " (" + localTime(t) + " your time)").join(" · ") }));
    } else {
      box.append(el("div", { class: "ink2" }, "Off. ", el("span", { class: "muted", text: "The schedule (" + r.form.text + ") is kept for when you turn it back on." })));
    }
    $("#sch-save").disabled = !!(current && current.broken) || keyOf(v) === savedKey;
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(preview, 250);
  }

  async function save() {
    const v = formValues();
    $("#sch-save").disabled = true;
    let r;
    try {
      r = await api("PUT", "/api/schedule", v);
    } catch (e) {
      const errs = (e.data && e.data.errors) || [e.message];
      $("#sch-status").textContent = "";
      clear($("#sch-preview")).append(el("div", { class: "msg error", text: errs.join(" ") }));
      $("#sch-save").disabled = false;
      return;
    }
    $("#sch-status").textContent = "Applying…";
    waitFor(r.id, Date.now());
  }

  async function waitFor(id, started) {
    clearTimeout(pollTimer);
    let s;
    try { s = await api("GET", "/api/schedule"); } catch (e) { s = null; }
    const st = s && s.last_status;
    if (st && st.id === id && !s.pending) {
      current = s;
      if (st.status === "applied") {
        toast("Schedule saved");
        $("#sch-status").textContent = "✓ Saved";
        fill(s.form, s.enabled, s.expr);
        lastExpr = s.expr;
        savedKey = JSON.stringify([s.enabled, s.expr]);
      } else {
        $("#sch-status").textContent = "";
        clear($("#sch-preview")).append(el("div", { class: "msg error", text: "Not saved: " + st.message }));
      }
      renderNotes();
      if (st.status === "applied") await preview();
      return;
    }
    if (Date.now() - started > 20000) {
      $("#sch-status").textContent = "Still waiting for the server. Is backup-portal-actions.path running on the host?";
      $("#sch-save").disabled = false;
      return;
    }
    pollTimer = setTimeout(() => waitFor(id, started), 1500);
  }

  for (const id of ["#sch-kind", "#sch-every", "#sch-enabled"]) {
    $(id).addEventListener("change", () => { showKind(); schedulePreview(); });
  }
  for (const id of ["#sch-time", "#sch-minute", "#sch-expr"]) $(id).addEventListener("input", schedulePreview);
  daysBox.addEventListener("change", schedulePreview);
  $("#sch-save").addEventListener("click", save);
  load();
})();
