"use strict";
/* Folder picker for one backup entry: choose which top-level folders get backed up,
   see how much space that needs, and remove unselected copies from the backup disk. */
function createFolderPicker() {
  const back = $("#picker");
  let entry = null, onApply = null, data = null, checked = new Set();
  let mode = "exclude", rootFiles = true, initialised = false;
  let pollTimer = null, lastFocus = null;

  const srcFolders = () => (data ? data.folders.filter(f => f.in_source) : []);
  const orphans = () => (data ? data.folders.filter(f => !f.in_source) : []);

  function savedUnchecked(name) {
    const ss = data && data.saved_selection;
    if (!ss) return false;
    if (ss.mode === "exclude") return ss.folders.includes(name);
    if (ss.mode === "include") return !ss.folders.includes(name);
    return false;
  }
  function removalFor(name) {
    return (data && data.removals || []).find(r => r.folder === name) || null;
  }

  async function scan(refresh) {
    const q = new URLSearchParams({ source: entry.source.trim(), destination: entry.destination.trim() });
    if (refresh) q.set("refresh", "1");
    $("#pk-status").textContent = "Measuring folder sizes…";
    try {
      data = await api("GET", "/api/folders?" + q);
    } catch (e) {
      data = null;
      $("#pk-status").textContent = e.message;
      render();
      return;
    }
    if (!initialised) {
      const names = srcFolders().map(f => f.name);
      checked = new Set(entry.mode === "include" ? names.filter(n => entry.folders.includes(n))
                       : entry.mode === "exclude" ? names.filter(n => !entry.folders.includes(n)) : names);
      initialised = true;
    }
    $("#pk-status").textContent = data.partial
      ? "Some folders are still being measured. Click Refresh sizes in a moment."
      : "Sizes measured " + relTime(data.sizes_as_of) + ".";
    render();
    schedulePoll();
  }

  function statusCell(f) {
    const r = removalFor(f.name);
    if (r && (r.status === "queued" || r.status === "running")) return el("span", { class: "pill running", text: "Removing from backup disk…" });
    if (r && r.status === "failed" && f.on_dest) return el("span", { class: "lvl-label critical", text: "✕ Removal failed: " + r.message });
    const on = checked.has(f.name);
    if (!f.in_source) return removeButton(f, "No longer in source");
    if (on) return el("span", { class: f.on_dest ? "lvl-label good" : "ink2", text: f.on_dest ? "✓ Backed up" : "Will be copied on next run" });
    if (!f.on_dest) return el("span", { class: "muted", text: r && r.status === "done" ? "Removed from backup disk" : "Not backed up" });
    if (savedUnchecked(f.name)) return removeButton(f, "Not selected");
    return el("span", { class: "small ink2", text: "Apply and save, then you can remove the copy" });
  }

  function removeButton(f, label) {
    const wrap = el("span", { class: "row" }, el("span", { class: "small ink2", text: label }));
    if (!f.on_dest) return wrap;
    if (!data.saved_selection) { wrap.append(el("span", { class: "small muted", text: "· save this backup first" })); return wrap; }
    const b = el("button", { class: "danger small-btn", text: "Remove copy (" + fmtBytes(f.dest_size) + ")" });
    b.addEventListener("click", () => confirmRemove(f));
    wrap.append(b);
    return wrap;
  }

  function render() {
    document.querySelectorAll("#pk-mode button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    $("#pk-mode-help").textContent = mode === "exclude"
      ? "Checked folders are backed up. Folders added to the source later are backed up automatically."
      : "Only checked folders are backed up. Folders added to the source later are NOT backed up until you check them.";
    $("#pk-root-wrap").hidden = mode !== "include";
    $("#pk-root").checked = rootFiles;
    $("#pk-root-label").textContent = "Also copy loose files at the top level" + (data ? " (" + fmtNum(data.loose.count) + " files, " + fmtBytes(data.loose.size) + ")" : "");

    // Rebuilding the list empties it for a moment, which makes the browser jump
    // to the top - so remember where the user was and put them back there.
    const scroller = back.querySelector(".modal .body");
    const scrollTop = scroller ? scroller.scrollTop : 0;
    const tb = clear($("#pk-rows"));
    const q = $("#pk-filter").value.trim().toLowerCase();
    let list = srcFolders().filter(f => !q || f.name.toLowerCase().includes(q));
    if ($("#pk-sort").value === "size") list = [...list].sort((a, b) => (b.src_size || 0) - (a.src_size || 0));
    for (const f of list) {
      const cb = el("input", { type: "checkbox", "aria-label": "Back up " + f.name });
      cb.checked = checked.has(f.name);
      const status = el("td", {}, statusCell(f));
      const tr = el("tr", { class: checked.has(f.name) ? "" : "off" },
        el("td", {}, el("label", { class: "pk-name" }, cb, el("span", { class: "name", text: f.name }))),
        el("td", { class: "num", text: fmtBytes(f.src_size) }),
        el("td", { class: "num", text: f.on_dest ? fmtBytes(f.dest_size) : "—" }),
        status);
      // A single click updates only this row and the totals - the list itself stays put.
      cb.addEventListener("change", () => {
        cb.checked ? checked.add(f.name) : checked.delete(f.name);
        tr.className = cb.checked ? "" : "off";
        clear(status).append(statusCell(f));
        renderFit();
      });
      tb.append(tr);
    }
    if (data && !list.length) tb.append(el("tr", {}, el("td", { colspan: 4, class: "muted", text: q ? "No folders match the filter." : "This source has no subfolders." })));

    const ob = clear($("#pk-orphans"));
    const orph = orphans();
    if (orph.length) {
      const otb = el("tbody");
      for (const f of orph) otb.append(el("tr", {}, el("td", { class: "mono", text: f.name }), el("td", { class: "num", text: fmtBytes(f.dest_size) }), el("td", {}, statusCell(f))));
      ob.append(el("h3", { text: "On the backup disk but no longer in the source (" + orph.length + ")" }),
        el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {}, el("th", { text: "Folder" }), el("th", { class: "num", text: "On backup disk" }), el("th", { text: "" }))), otb)));
    }
    if (scroller) scroller.scrollTop = scrollTop;
    renderFit();
  }

  function renderFit() {
    const box = clear($("#pk-fit"));
    if (!data) return;
    const sel = srcFolders().filter(f => checked.has(f.name));
    const unknown = sel.some(f => f.src_size === null);
    const looseIncluded = mode === "exclude" || rootFiles;
    const selBytes = sel.reduce((s, f) => s + (f.src_size || 0), 0) + (looseIncluded ? data.loose.size : 0);
    const toCopy = sel.reduce((s, f) => s + Math.max(0, (f.src_size || 0) - (f.on_dest ? (f.dest_size || 0) : 0)), 0);
    const reclaim = data.folders.filter(f => f.on_dest && (!f.in_source || !checked.has(f.name)))
      .reduce((s, f) => s + (f.dest_size || 0), 0);
    const free = data.dest_fs ? data.dest_fs.avail : null;
    const fits = free === null ? null : toCopy <= free;
    const stat = (label, value) => el("div", { class: "pk-stat" }, el("div", { class: "label", text: label }), el("div", { class: "value", text: value }));
    box.append(el("div", { class: "pk-stats" },
      stat("Selected", fmtNum(sel.length) + " of " + fmtNum(srcFolders().length) + " · " + fmtBytes(selBytes) + (unknown ? "+" : "")),
      stat("Still to copy", fmtBytes(toCopy)),
      stat("Free on " + (data.dest_fs ? data.dest_fs.mount : "backup disk"), free === null ? "—" : fmtBytes(free)),
      stat("Unselected copies on disk", fmtBytes(reclaim))));
    let verdict;
    if (fits === null) verdict = el("span", { class: "muted", text: "Free space unknown" });
    else if (fits) verdict = el("span", { class: "lvl-label good", text: "✓ The selection fits" + (toCopy ? " (" + fmtBytes(free - toCopy) + " left after the next run)" : "") });
    else verdict = el("span", { class: "lvl-label critical", text: "✕ Needs " + fmtBytes(toCopy - free) + " more than is free" + (reclaim ? ". Removing unselected copies would free " + fmtBytes(reclaim) : "") });
    box.append(el("div", { class: "row" }, verdict,
      el("span", { class: "small muted", text: "Free space is shared with any other backups that use this disk." })));
  }

  function confirmRemove(f) {
    const dlg = $("#pk-confirm");
    $("#pkc-text").textContent = "This permanently deletes the backup copy of “" + f.name + "” (" + fmtBytes(f.dest_size) + ") from " +
      data.dest_folder + ". The original in " + data.source + " is not touched" + (f.in_source ? ", but it will no longer have a backup." : ".");
    $("#pkc-name").textContent = f.name;
    const input = $("#pkc-input");
    input.value = "";
    const go = $("#pkc-go");
    go.disabled = true;
    input.oninput = () => { go.disabled = input.value !== f.name; };
    go.onclick = async () => {
      go.disabled = true;
      try {
        await api("POST", "/api/remove-dest-folder", { source: data.source, destination: data.destination, folder: f.name });
        toast("Removing “" + f.name + "” from the backup disk");
        data.removals = [{ folder: f.name, status: "queued", updated: Date.now() / 1000 }, ...(data.removals || [])];
        dlg.hidden = true;
        render();
        schedulePoll();
      } catch (e) { toast(e.message, "error"); go.disabled = false; }
    };
    $("#pkc-cancel").onclick = () => { dlg.hidden = true; };
    dlg.hidden = false;
    input.focus();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    const active = (data && data.removals || []).some(r => r.status === "queued" || r.status === "running");
    if (!active || back.hidden) return;
    pollTimer = setTimeout(async () => {
      try {
        const r = await api("GET", "/api/removals");
        const mine = r.removals.filter(x => x.source === data.source && x.destination === data.destination);
        const finished = mine.some(x => x.status === "done" || x.status === "failed");
        data.removals = mine;
        if (finished && !mine.some(x => x.status === "queued" || x.status === "running")) { await scan(false); return; }
        render();
      } catch (e) { /* retry */ }
      schedulePoll();
    }, 3000);
  }

  function close() {
    back.hidden = true;
    $("#pk-confirm").hidden = true;
    clearTimeout(pollTimer);
    if (lastFocus) lastFocus.focus();
  }

  function apply() {
    const names = srcFolders().map(f => f.name);
    if (mode === "include") {
      const inc = names.filter(n => checked.has(n));
      if (!inc.length) { toast("Check at least one folder, or switch to “Everything except unchecked”.", "error"); return; }
      entry.mode = "include"; entry.folders = inc; entry.include_root_files = rootFiles;
    } else {
      const exc = names.filter(n => !checked.has(n));
      entry.mode = exc.length ? "exclude" : "all"; entry.folders = exc; entry.include_root_files = true;
    }
    close();
    onApply && onApply();
    toast("Selection updated. Save to config.yaml to apply it.");
  }

  document.querySelectorAll("#pk-mode button").forEach(b => b.addEventListener("click", () => { mode = b.dataset.mode; render(); }));
  $("#pk-root").addEventListener("change", (e) => { rootFiles = e.target.checked; render(); });
  $("#pk-filter").addEventListener("input", render);
  $("#pk-sort").addEventListener("change", render);
  const visible = () => srcFolders().filter(f => { const q = $("#pk-filter").value.trim().toLowerCase(); return !q || f.name.toLowerCase().includes(q); });
  $("#pk-all").addEventListener("click", () => { visible().forEach(f => checked.add(f.name)); render(); });
  $("#pk-none").addEventListener("click", () => { visible().forEach(f => checked.delete(f.name)); render(); });
  $("#pk-refresh").addEventListener("click", () => scan(true));
  $("#pk-cancel").addEventListener("click", close);
  $("#pk-apply").addEventListener("click", apply);
  back.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#pk-confirm").hidden) $("#pk-confirm").hidden = true; else close();
  });
  $("#pk-confirm").addEventListener("keydown", (e) => { if (e.key === "Escape") $("#pk-confirm").hidden = true; });

  return {
    open(b, cb) {
      entry = b; onApply = cb; data = null; initialised = false;
      mode = b.mode === "include" ? "include" : "exclude";
      rootFiles = b.include_root_files !== false;
      lastFocus = document.activeElement;
      $("#pk-h").textContent = "Choose folders: " + (b.source.split("/").filter(Boolean).pop() || b.source);
      $("#pk-sub").textContent = b.source + " → " + b.destination;
      $("#pk-filter").value = "";
      clear($("#pk-rows")); clear($("#pk-orphans")); clear($("#pk-fit"));
      back.hidden = false;
      $("#pk-filter").focus();
      scan(false);
    },
  };
}
