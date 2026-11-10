"use strict";
(() => {
  let server = null;          // last loaded config (with metadata)
  let model = null;           // editable copy
  let validateTimer = null;

  const selKey = (b) => b.mode === "all" ? ["all"] : [b.mode, [...b.folders].sort(), b.mode === "include" ? b.include_root_files : true];
  const snapshot = (m) => JSON.stringify({ b: m.backups.map(b => [b.source.trim(), b.destination.trim(), selKey(b)]), m: m.mounts.map(x => x.trim()).filter(Boolean), r: m.rsync_opts.trim().split(/\s+/).join(" ") });
  const isDirty = () => server && model && snapshot(model) !== snapshot(fromServer(server));

  function fromServer(cfg) {
    return {
      backups: cfg.backups.map(b => ({ source: b.source, destination: b.destination, mode: b.mode, folders: [...b.folders], include_root_files: b.include_root_files, meta: b })),
      mounts: [...cfg.required_mount_points],
      rsync_opts: cfg.rsync_opts || "",
    };
  }

  async function load() {
    try {
      server = await api("GET", "/api/config");
    } catch (e) {
      clear($("#load-error")).append(el("div", { class: "msg error", text: "Could not load config: " + e.message }));
      return;
    }
    model = fromServer(server);
    $("#cfg-path").textContent = "Version " + server.version;
    const copies = server.backup_copies;
    $("#copies").textContent = copies.length ? "Each save keeps a copy of the previous file in config_backups/ (latest: " + copies[0] + ")." : "Each save keeps a copy of the previous file in config_backups/.";
    clear($("#messages"));
    render();
  }

  function render() {
    renderBackups();
    renderMounts();
    $("#rsync-opts").value = model.rsync_opts;
    updateDirty();
  }

  function pathField(label, value, onInput, browseStart) {
    const input = el("input", { type: "text", class: "path", value, spellcheck: "false", "aria-label": label });
    input.addEventListener("input", () => onInput(input.value));
    const btn = el("button", { type: "button", title: "Browse", text: "Browse…" });
    btn.addEventListener("click", () => browser.open(input.value || browseStart, (p) => { input.value = p; onInput(p); }));
    return el("div", { class: "field" }, el("label", { text: label }), el("div", { class: "input-row" }, input, btn));
  }

  function rowMeta(b) {
    const m = b.meta;
    const meta = el("div", { class: "meta" });
    const changed = !m || m.source !== b.source || m.destination !== b.destination;
    if (!m) {
      meta.append(el("span", { text: "New entry. The first run copies the whole folder, so check that the destination has room." }));
      return meta;
    }
    if (m.last && !changed) {
      meta.append(el("span", { text: "Source size " + fmtBytes(m.last.total_size) + " · " + fmtNum(m.last.files) + " files" }));
      if (m.growth_per_day) meta.append(el("span", { text: (m.growth_per_day >= 0 ? "+" : "") + fmtBytes(m.growth_per_day) + "/day" }));
      meta.append(el("span", { class: "muted", text: "last backed up " + relTime(m.last.end) }));
    } else if (!changed) {
      meta.append(el("span", { class: "muted", text: "No successful backup recorded yet" }));
    }
    if (m.dest_fs && !changed) {
      const f = m.dest_fs;
      meta.append(el("span", { class: "lvl-label " + f.level, text: levelIcon(f.level) + "Destination " + f.mount + ": " + fmtBytes(f.avail) + " free (" + f.pct.toFixed(0) + "% used)" }));
    }
    if (changed) meta.append(el("span", { class: "muted", text: "Changed. Stats appear after the next run." }));
    return meta;
  }

  function renderBackups() {
    const box = clear($("#backups"));
    if (!model.backups.length) box.append(el("p", { class: "muted", text: "No backups configured. Add one to get started." }));
    model.backups.forEach((b, i) => {
      const row = el("div", { class: "sel-row" + (b.meta ? "" : " new") });
      const meta = rowMeta(b);
      const src = pathField("Source folder", b.source, v => { b.source = v; changed(); }, "/mnt");
      const dst = pathField("Destination", b.destination, v => { b.destination = v; changed(); }, "/mnt");
      const up = el("button", { class: "icon", title: "Move up", "aria-label": "Move up", text: "↑", disabled: i === 0 });
      const down = el("button", { class: "icon", title: "Move down", "aria-label": "Move down", text: "↓", disabled: i === model.backups.length - 1 });
      const del = el("button", { class: "icon danger", title: "Remove", "aria-label": "Remove backup", text: "✕" });
      up.addEventListener("click", () => { [model.backups[i - 1], model.backups[i]] = [model.backups[i], model.backups[i - 1]]; render(); scheduleValidate(); });
      down.addEventListener("click", () => { [model.backups[i + 1], model.backups[i]] = [model.backups[i], model.backups[i + 1]]; render(); scheduleValidate(); });
      del.addEventListener("click", () => { model.backups.splice(i, 1); render(); scheduleValidate(); });
      const left = el("div", {}, src, meta);
      const sel = el("div", { class: "folders-line" });
      const right = el("div", {}, dst, sel);
      const renderSel = () => {
        clear(sel).append(el("span", { class: "sel-summary" + (b.mode === "all" ? "" : " active"), text: selSummary(b) }));
        const btn = el("button", { type: "button", text: "Choose folders…", disabled: !b.source.trim().startsWith("/") || !b.destination.trim().startsWith("/") });
        btn.addEventListener("click", () => picker.open(b, () => { renderSel(); updateDirty(); scheduleValidate(); }));
        const saved = savedUnchanged(b);
        const cu = el("button", { type: "button", class: "danger", text: "Backup Folder Cleanup", disabled: !saved,
          title: saved ? "Delete from the backup everything that no longer exists in the source"
                       : "Save your changes to this backup set first" });
        cu.addEventListener("click", () => cleanupDialog.open(b));
        sel.append(btn, cu);
      };
      renderSel();
      row.append(el("div", { class: "idx", text: String(i + 1) }), left, right, el("div", { class: "ops" }, up, down, del));
      box.append(row);
      function changed() { const n = rowMeta(b); left.replaceChild(n, left.lastChild); renderSel(); updateDirty(); scheduleValidate(); }
    });
  }

  function renderMounts() {
    const box = clear($("#mounts"));
    if (!model.mounts.length) box.append(el("p", { class: "small muted", text: "None. Backups run even if a disk is missing." }));
    model.mounts.forEach((mp, i) => {
      const input = el("input", { type: "text", class: "path", value: mp, spellcheck: "false", "aria-label": "Mount point " + (i + 1) });
      input.addEventListener("input", () => { model.mounts[i] = input.value; updateDirty(); scheduleValidate(); });
      const mounted = server.mounts.includes(mp);
      const pill = el("span", { class: "pill " + (mounted ? "good" : "critical"), text: mounted ? "Mounted" : "Not mounted" });
      const del = el("button", { class: "icon danger", title: "Remove", "aria-label": "Remove mount point", text: "✕" });
      del.addEventListener("click", () => { model.mounts.splice(i, 1); renderMounts(); updateDirty(); scheduleValidate(); });
      box.append(el("div", { class: "item" }, input, pill, del));
    });
  }

  function updateDirty() {
    const d = isDirty();
    $("#save").disabled = !d;
    $("#revert").disabled = !d;
    $("#dirty-note").textContent = d ? "You have unsaved changes" : "No unsaved changes";
  }

  function payload(force) {
    return {
      version: server.version,
      backups: model.backups.map(b => ({ source: b.source.trim(), destination: b.destination.trim(), mode: b.mode, folders: b.folders, include_root_files: b.include_root_files })),
      required_mount_points: model.mounts.map(m => m.trim()).filter(Boolean),
      rsync_opts: model.rsync_opts,
      force: !!force,
    };
  }

  function showMessages(errors, warnings, saveAnyway) {
    const box = clear($("#messages"));
    if (errors && errors.length) {
      box.append(el("div", { class: "msg error", role: "alert" }, el("strong", { text: "Fix these before saving" }),
        el("ul", {}, errors.map(e => el("li", { text: e })))));
    }
    if (warnings && warnings.length) {
      const w = el("div", { class: "msg warn" }, el("strong", { text: "Check these" }), el("ul", {}, warnings.map(e => el("li", { text: e }))));
      if (saveAnyway) {
        const b = el("button", { class: "primary", text: "Save anyway" });
        b.addEventListener("click", () => save(true));
        w.append(el("div", { class: "row" }, b));
      }
      box.append(w);
    }
  }

  function scheduleValidate() {
    clearTimeout(validateTimer);
    validateTimer = setTimeout(async () => {
      if (!isDirty()) { clear($("#messages")); return; }
      try {
        const r = await api("POST", "/api/config/validate", payload(false));
        showMessages(r.errors, r.warnings, false);
      } catch (e) { /* ignore transient */ }
    }, 500);
  }

  async function save(force) {
    $("#save").disabled = true;
    try {
      const r = await api("PUT", "/api/config", payload(force));
      toast("Saved. The next backup run will use these settings.");
      await load();
      if (r.warnings && r.warnings.length) showMessages([], r.warnings, false);
    } catch (e) {
      if (e.status === 422 && e.data) {
        showMessages(e.data.errors, e.data.warnings, !(e.data.errors || []).length);
      } else if (e.status === 409) {
        const box = clear($("#messages"));
        const b = el("button", { text: "Reload (discards your edits)" });
        b.addEventListener("click", load);
        box.append(el("div", { class: "msg error" }, el("strong", { text: e.message }), b));
      } else {
        toast("Save failed: " + e.message, "error");
      }
      updateDirty();
    }
  }

  $("#add-backup").addEventListener("click", () => {
    model.backups.push({ source: "", destination: "", mode: "all", folders: [], include_root_files: true, meta: null });
    render();
    const inputs = document.querySelectorAll("#backups .sel-row:last-child input");
    if (inputs[0]) inputs[0].focus();
  });
  $("#add-mount").addEventListener("click", () => { model.mounts.push(""); renderMounts(); updateDirty(); });
  $("#rsync-opts").addEventListener("input", (e) => { model.rsync_opts = e.target.value; updateDirty(); scheduleValidate(); });
  $("#save").addEventListener("click", () => save(false));
  $("#revert").addEventListener("click", () => { model = fromServer(server); clear($("#messages")); render(); });
  window.addEventListener("beforeunload", (e) => { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });

  /* ---------- folder browser modal ---------- */
  const browser = (() => {
    let current = null, cb = null, lastFocus = null;
    const back = $("#browser");
    async function go(path) {
      const list = clear($("#br-list"));
      try {
        const d = await api("GET", "/api/browse?path=" + encodeURIComponent(path));
        current = d.path;
        $("#br-current").textContent = d.path;
        const crumbs = clear($("#br-crumbs"));
        crumbs.style.padding = "8px 12px";
        const root = d.roots.find(r => d.path === r || d.path.startsWith(r + "/")) || d.roots[0];
        let acc = root;
        const rootBtn = el("button", { text: root });
        rootBtn.addEventListener("click", () => go(root));
        crumbs.append(rootBtn);
        for (const part of d.path.slice(root.length).split("/").filter(Boolean)) {
          acc = acc + "/" + part;
          const p = acc;
          const b = el("button", { text: part });
          b.addEventListener("click", () => go(p));
          crumbs.append(el("span", { class: "muted", text: "/" }), b);
        }
        if (d.parent) {
          const up = el("button", {}, el("span", { class: "folder", text: "↰" }), "..");
          up.addEventListener("click", () => go(d.parent));
          list.append(up);
        }
        for (const name of d.dirs) {
          const b = el("button", {}, el("span", { class: "folder", text: "▸" }), name);
          b.addEventListener("click", () => go(d.path.replace(/\/$/, "") + "/" + name));
          list.append(b);
        }
        if (!d.dirs.length) list.append(el("p", { class: "muted small", text: "No subfolders" }));
        const first = list.querySelector("button");
        if (first) first.focus();
      } catch (e) {
        list.append(el("div", { class: "msg error", text: e.message }));
        if (e.status === 404 || e.status === 403) {
          const b = el("button", { text: "Go to /mnt" });
          b.addEventListener("click", () => go("/mnt"));
          list.append(b);
        }
      }
    }
    function close() { back.hidden = true; if (lastFocus) lastFocus.focus(); }
    $("#br-cancel").addEventListener("click", close);
    $("#br-select").addEventListener("click", () => { if (cb && current) cb(current); close(); });
    back.addEventListener("click", (e) => { if (e.target === back) close(); });
    back.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    return {
      open(start, callback) {
        cb = callback; lastFocus = document.activeElement;
        back.hidden = false;
        go(start && start.startsWith("/") ? start : "/mnt");
      },
    };
  })();

  function selSummary(b) {
    const n = b.folders.length;
    if (b.mode === "exclude") return "Folders: all except " + n + " (" + b.folders.slice(0, 3).join(", ") + (n > 3 ? ", …" : "") + ")";
    if (b.mode === "include") return "Folders: only " + n + " selected" + (b.include_root_files ? "" : ", no loose files");
    return "Folders: all";
  }

  /* Cleanup works on the saved config.yaml, so it is only offered for saved, unchanged sets. */
  function savedUnchanged(b) {
    const m = b.meta;
    return !!m && m.source === b.source.trim() && m.destination === b.destination.trim() &&
      JSON.stringify(selKey(b)) === JSON.stringify(selKey(m));
  }

  /* ---------- folder picker modal ---------- */
  const picker = createFolderPicker();
  const cleanupDialog = createCleanupDialog();

  load();
})();
