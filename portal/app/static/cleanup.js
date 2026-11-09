"use strict";
/* Backup Folder Cleanup: remove from a backup folder everything that no longer exists
   in its source. Step 1 compares (read-only), step 2 needs the folder name typed,
   step 3 hands the plan to the host, which re-checks every path before deleting. */
function createCleanupDialog() {
  const back = $("#cleanup");
  let plan = null, pollTimer = null, lastFocus = null, onFinished = null;

  function close() {
    back.hidden = true;
    clearTimeout(pollTimer);
    if (lastFocus) lastFocus.focus();
  }

  function setBusy(text) {
    clear($("#cu-body")).append(el("p", { class: "ink2", text }));
    $("#cu-confirm").hidden = true;
  }

  function showError(message) {
    clear($("#cu-body")).append(el("div", { class: "msg error", role: "alert", text: message }));
    $("#cu-confirm").hidden = true;
  }

  function renderPlan() {
    const body = clear($("#cu-body"));
    const p = plan;
    if (!p.total_items) {
      body.append(el("div", { class: "msg info" }, el("strong", { text: "✓ Nothing to clean up" }),
        "Everything in " + p.dest_folder + " still exists in " + p.source + "."));
      if (p.protected.length) body.append(protectedNote(p));
      $("#cu-confirm").hidden = true;
      return;
    }
    const pct = Math.round(p.share_of_backup * 100);
    body.append(el("div", { class: "pk-stats" },
      stat("Items to delete", fmtNum(p.total_items)),
      stat("Files inside them", fmtNum(p.total_files)),
      stat("Space freed", fmtBytes(p.total_size)),
      stat("Share of this backup", (pct < 1 && p.total_size ? "<1" : pct) + "%")));
    if (p.share_of_backup > 0.25) {
      body.append(el("div", { class: "msg error", role: "alert" },
        el("strong", { text: "✕ This would delete " + pct + "% of the backup" }),
        "Make sure " + p.source + " is complete and its disk is healthy before continuing."));
    }
    if (p.protected.length) body.append(protectedNote(p));
    const tb = el("tbody");
    for (const it of p.items) {
      tb.append(el("tr", {}, el("td", { class: "path", text: it.path + (it.type === "folder" ? "/" : "") }),
        el("td", { class: "ink2", text: it.type === "folder" ? "Folder · " + fmtNum(it.files) + " files" : it.type === "link" ? "Link" : "File" }),
        el("td", { class: "num", text: fmtBytes(it.size) })));
    }
    body.append(el("h3", { text: "In the backup but no longer in the source" }),
      el("div", { class: "table-wrap" }, el("table", { class: "pk-table" },
        el("thead", {}, el("tr", {}, el("th", { text: "Path" }), el("th", { text: "Type" }), el("th", { class: "num", text: "Size" }))),
        tb)));
    if (p.items_truncated) body.append(el("p", { class: "small muted", text: "Showing the first " + fmtNum(p.items.length) + " of " + fmtNum(p.total_items) + " items. All of them will be deleted." }));

    $("#cu-confirm").hidden = false;
    $("#cu-name").textContent = p.confirm_name;
    const input = $("#cu-input"), go = $("#cu-go");
    input.value = "";
    go.textContent = "Delete " + fmtNum(p.total_items) + " item" + (p.total_items === 1 ? "" : "s") + " (" + fmtBytes(p.total_size) + ")";
    go.disabled = true;
    input.oninput = () => { go.disabled = input.value !== p.confirm_name; };
    go.onclick = start;
  }

  function stat(label, value) {
    return el("div", { class: "pk-stat" }, el("div", { class: "label", text: label }), el("div", { class: "value", text: value }));
  }

  function protectedNote(p) {
    return el("p", { class: "small ink2", text: "Not touched (deselected for backup, still in the source): " +
      p.protected.slice(0, 8).join(", ") + (p.protected.length > 8 ? " and " + (p.protected.length - 8) + " more" : "") + "." });
  }

  async function start() {
    const go = $("#cu-go");
    go.disabled = true;
    try {
      await api("POST", "/api/cleanup/run", { plan_id: plan.id, confirm: $("#cu-input").value });
    } catch (e) {
      toast(e.message, "error");
      go.disabled = false;
      return;
    }
    $("#cu-confirm").hidden = true;
    clear($("#cu-body")).append(el("p", {}, el("span", { class: "pill running", text: "Cleaning up…" })),
      el("p", { class: "small ink2", id: "cu-progress", text: "Waiting for the server" }));
    poll();
  }

  async function poll() {
    clearTimeout(pollTimer);
    let st;
    try { st = await api("GET", "/api/cleanup/" + plan.id); } catch (e) { st = null; }
    if (st && (st.status === "done" || st.status === "failed")) return finished(st);
    const pr = $("#cu-progress");
    if (st && pr) pr.textContent = st.message || "Working…";
    if (!back.hidden) pollTimer = setTimeout(poll, 2000);
  }

  function finished(st) {
    const body = clear($("#cu-body"));
    if (st.status === "done") {
      body.append(el("div", { class: "msg info" }, el("strong", { text: "✓ Cleanup finished" }),
        "Deleted " + fmtNum(st.deleted) + " item" + (st.deleted === 1 ? "" : "s") + ", freed " + fmtBytes(st.freed) + "."));
    } else {
      body.append(el("div", { class: "msg error", role: "alert" }, el("strong", { text: "✕ Cleanup refused" }), st.message || ""));
    }
    if (st.skipped) {
      const ul = el("ul", {}, (st.skipped_items || []).slice(0, 50).map(s => el("li", {}, el("span", { class: "mono", text: s.path }), " - " + s.reason)));
      body.append(el("div", { class: "msg warn" }, el("strong", { text: fmtNum(st.skipped) + " item(s) were skipped and kept" }), ul));
    }
    onFinished && onFinished();
  }

  $("#cu-close").addEventListener("click", close);
  back.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

  return {
    async open(b, cb) {
      lastFocus = document.activeElement;
      onFinished = cb;
      plan = null;
      const name = b.source.split("/").filter(Boolean).pop() || b.source;
      $("#cu-h").textContent = "Backup Folder Cleanup: " + name;
      $("#cu-sub").textContent = "Deletes from " + b.destination.replace(/\/$/, "") + "/" + name +
        " everything that no longer exists in " + b.source + ".";
      back.hidden = false;
      setBusy("Comparing the backup with the source… nothing is deleted yet.");
      $("#cu-close").focus();
      try {
        plan = await api("POST", "/api/cleanup/plan", { source: b.source, destination: b.destination });
      } catch (e) {
        showError(e.message);
        return;
      }
      renderPlan();
      if (!$("#cu-confirm").hidden) $("#cu-input").focus();
    },
  };
}
