/* Browser tests for the portal pages, run in jsdom against fixtures rendered by
 * make_fixtures.py. fetch() is stubbed with the recorded API responses; every
 * non-GET request is captured so tests can assert on what would be sent.
 *
 * Usage: node tests/js/run.js FIXTURE_DIR
 */
"use strict";
const { JSDOM, VirtualConsole } = require("jsdom");
const fs = require("fs");
const path = require("path");
const assert = require("assert/strict");

const FX = process.argv[2];
const STATIC = path.resolve(__dirname, "../../portal/app/static");
const API = JSON.parse(fs.readFileSync(path.join(FX, "api.json"), "utf8"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function loadPage(page, { api = API, respond } = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push("jsdomError: " + e.message));
  vc.on("error", (e) => errors.push("console.error: " + e));
  const html = fs.readFileSync(path.join(FX, page + ".html"), "utf8");
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "https://portal.test/" + page,
                                pretendToBeVisual: true, virtualConsole: vc });
  const w = dom.window;
  const sent = [], previews = [];
  const data = JSON.parse(JSON.stringify(api));
  w.fetch = async (url, opts = {}) => {
    const u = new URL(url, "https://portal.test");
    const method = opts.method || "GET";
    let p = u.pathname.startsWith("/api/history/run/") ? "/api/history/run" : u.pathname;
    // previews have no side effects, so they are tracked apart from the requests that change things
    const rec = { method, path: u.pathname, body: opts.body ? JSON.parse(opts.body) : null,
                  csrf: opts.headers && opts.headers["X-CSRF-Token"] };
    if (u.pathname === "/api/schedule/preview") previews.push(rec);
    else if (method !== "GET") sent.push(rec);
    let status = 200, body;
    const custom = respond && respond(method, p, opts.body ? JSON.parse(opts.body) : null);
    if (custom) ({ status, body } = custom);
    else if (method === "GET") body = data[p];
    else if (data[method + " " + p] !== undefined) body = data[method + " " + p];
    else body = { ok: true, id: "0".repeat(32), version: "v2", warnings: [] };
    if (body === undefined) errors.push("no fixture for " + method + " " + p);
    return { ok: status < 400, status, json: async () => JSON.parse(JSON.stringify(body)) };
  };
  w.ResizeObserver = class {
    constructor(cb) { this.cb = cb; }
    observe(el) { Object.defineProperty(el, "clientWidth", { value: 800, configurable: true }); setTimeout(() => this.cb([]), 0); }
    disconnect() {}
  };
  w.addEventListener("error", (e) => errors.push("error: " + e.message));
  w.addEventListener("unhandledrejection", (e) => errors.push("unhandled rejection: " + (e.reason && e.reason.message)));
  // classic <script> tags share one global scope, so evaluate them together
  const code = [...w.document.querySelectorAll("script[src]")]
    .map((s) => fs.readFileSync(path.join(STATIC, new URL(s.src).pathname.split("/").pop()), "utf8")).join("\n;\n");
  w.eval(code);
  await sleep(300);
  const d = w.document;
  const $ = (s) => d.querySelector(s);
  const $$ = (s) => [...d.querySelectorAll(s)];
  const text = (s) => ($(s) ? $(s).textContent.replace(/\s+/g, " ").trim() : "");
  const fire = (el, type) => el.dispatchEvent(new w.Event(type, { bubbles: true }));
  return { w, d, $, $$, text, fire, sent, previews, errors, data };
}

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

/* ------------------------------------------------------------------ every page */
for (const page of ["dashboard", "selections", "history", "filesystems"]) {
  test(`${page}: loads without script errors`, async () => {
    const p = await loadPage(page);
    assert.deepEqual(p.errors, []);
    assert.ok(!p.text("main").includes("null"), "no stray 'null' text");
    assert.ok(!p.text("main").includes("undefined"), "no stray 'undefined' text");
    assert.ok(!p.text("main").includes("NaN"), "no NaN values");
  });
}

/* ------------------------------------------------------------------ dashboard */
test("dashboard: healthy status, schedule, last run and log", async () => {
  const p = await loadPage("dashboard");
  assert.match(p.text("#status-main"), /Backups healthy/);
  assert.match(p.text("#status-flags"), /Next scheduled run/);
  assert.match(p.text("#last-run"), /Success/);
  assert.ok(p.$$("#log div").length > 0);
  assert.ok(p.$$("#log .err").length >= 1, "errors highlighted");
  assert.ok(p.$$("#fs-tiles .fs-tile").length >= 2);
});

test("dashboard: buttons send the right actions with the CSRF token", async () => {
  const p = await loadPage("dashboard");
  p.$("#btn-run").click();
  p.$("#btn-stop").click();
  await sleep(50);
  assert.deepEqual(p.sent.map((s) => s.path), ["/api/action/run", "/api/action/stop_on"]);
  const token = p.$('meta[name="csrf-token"]').content;
  assert.ok(token);
  assert.ok(p.sent.every((s) => s.csrf === token), "every request carries the page's CSRF token");
});

test("dashboard: paused state disables Run now and offers Resume", async () => {
  const api = JSON.parse(JSON.stringify(API));
  api["/api/status"].flags.stopped = { path: "/mnt/.backup_stop", since: Date.now() / 1000 - 60 };
  const p = await loadPage("dashboard", { api });
  assert.match(p.text("#status-main"), /paused/);
  assert.equal(p.$("#btn-run").disabled, true);
  assert.equal(p.$("#btn-stop").textContent, "Resume scheduled backups");
  p.$("#btn-stop").click();
  await sleep(50);
  assert.equal(p.sent[0].path, "/api/action/stop_off");
});

test("dashboard: failed state shows reason and Clear button", async () => {
  const api = JSON.parse(JSON.stringify(API));
  api["/api/status"].flags.failed = { path: "/mnt/.backup_failed", since: Date.now() / 1000 };
  const p = await loadPage("dashboard", { api });
  assert.match(p.text("#status-main"), /Last backup failed/);
  assert.equal(p.$("#btn-clear").hidden, false);
});

test("dashboard: missing cron entry is called out", async () => {
  const api = JSON.parse(JSON.stringify(API));
  api["/api/status"].schedules = [];
  const p = await loadPage("dashboard", { api });
  assert.match(p.text("#status-flags"), /Automatic backups are off/);
  assert.equal(p.$("#status-flags a").getAttribute("href"), "/selections#schedule");
});

test("dashboard: action errors are shown", async () => {
  const p = await loadPage("dashboard", {
    respond: (m, path) => (m === "POST" ? { status: 409, body: { error: "A backup is already running" } } : null) });
  p.$("#btn-run").click();
  await sleep(50);
  assert.match(p.text("#toast"), /already running/);
});

/* ------------------------------------------------------------------ history */
test("history: tiles, four charts and the runs table", async () => {
  const p = await loadPage("history");
  assert.match(p.text("#tiles"), /Success rate/);
  assert.equal(p.$$(".chart svg").length, 4);
  assert.ok(p.$$("#ch-bytes .bar").length > 0);
  assert.equal(p.$$("#runs tr.clickable").length, API["/api/history/runs"].runs.length);
});

test("history: clicking a run shows its jobs", async () => {
  const p = await loadPage("history");
  p.$("#runs tr.clickable").click();
  await sleep(100);
  assert.ok(p.$("tr.detail"));
  assert.match(p.text("tr.detail"), /TV/);
  p.$("#runs tr.clickable").click();
  assert.equal(p.$("tr.detail"), null, "second click collapses");
});

/* ------------------------------------------------------------------ filesystems */
test("filesystems: one row per filesystem, roles shown", async () => {
  const p = await loadPage("filesystems");
  assert.equal(p.$$("#fs-rows tr").length, API["/api/filesystems"].filesystems.length);
  assert.match(p.text("#fs-rows"), /Backup destination/);
  assert.ok(p.$$("#ch-trend svg").length === 1);
});

/* ------------------------------------------------------------------ selections */
const openPicker = async (p, idx = 0) => {
  p.$$(".folders-line button")[idx].click();
  await sleep(200);
};
const pickerRow = (p, name) => p.$$("#pk-rows tr").find((r) => r.querySelector(".name") && r.querySelector(".name").textContent === name);
const uncheck = (p, name) => { const cb = pickerRow(p, name).querySelector("input"); cb.checked = false; p.fire(cb, "change"); };

test("selections: rows, summaries, no unsaved changes at start", async () => {
  const p = await loadPage("selections");
  assert.equal(p.$$(".sel-row").length, 2);
  assert.deepEqual(p.$$(".sel-summary").map((e) => e.textContent), ["Folders: all", "Folders: all"]);
  assert.equal(p.$("#save").disabled, true);
});

test("selections: picker lists folders with sizes and orphans", async () => {
  const p = await loadPage("selections");
  await openPicker(p);
  assert.equal(p.$("#picker").hidden, false);
  const names = p.$$("#pk-rows .name").map((e) => e.textContent);
  assert.ok(names.includes("Big Show") && names.includes("New Show"));
  assert.ok(!names.includes("Gone Show"), "orphans are listed separately");
  assert.match(p.text("#pk-orphans"), /Gone Show/);
  assert.ok(p.$$("#pk-rows input:checked").length === names.length, "everything selected in 'all' mode");
  assert.match(p.text("#pk-fit"), /fits/);
});

test("selections: folder names are shown as text, never HTML", async () => {
  const p = await loadPage("selections");
  await openPicker(p);
  assert.equal(p.w.__xss, undefined);
  assert.equal(p.$$("#pk-rows img").length, 0);
  assert.ok(pickerRow(p, '<img src=x onerror="window.__xss=1">'));
});

test("selections: unchecking updates totals; apply + save sends an exclude list", async () => {
  const p = await loadPage("selections");
  await openPicker(p);
  const before = p.text("#pk-fit");
  uncheck(p, "Big Show");
  assert.notEqual(p.text("#pk-fit"), before);
  assert.match(p.text("#pk-fit"), /Unselected copies on disk/);
  // Big Show's copy (8,000 B) + the orphan Gone Show (250 B) = 8,250 B
  assert.match(p.text("#pk-fit"), /Unselected copies on disk ?8\.1 KiB/);
  assert.match(p.text("#pk-fit"), /Selected ?4 of 5/);
  p.$("#pk-apply").click();
  await sleep(50);
  assert.equal(p.$("#picker").hidden, true);
  assert.match(p.$$(".sel-summary")[0].textContent, /all except 1 \(Big Show\)/);
  assert.equal(p.$("#save").disabled, false);
  p.$("#save").click();
  await sleep(100);
  const put = p.sent.find((s) => s.method === "PUT");
  assert.ok(put, "PUT /api/config sent");
  assert.equal(put.body.backups[0].mode, "exclude");
  assert.deepEqual(put.body.backups[0].folders, ["Big Show"]);
  assert.equal(put.body.backups[1].mode, "all");
  assert.equal(put.body.force, false);
});

test("selections: clicking a checkbox updates that row in place (no list rebuild)", async () => {
  // Regression: every click rebuilt the whole list, which made the browser jump to the top.
  const p = await loadPage("selections");
  await openPicker(p);
  const row = pickerRow(p, "Small Show");
  const cb = row.querySelector("input");
  const others = p.$$("#pk-rows tr");
  cb.focus();
  const fitBefore = p.text("#pk-fit");
  cb.checked = false; p.fire(cb, "change");
  assert.ok(row.isConnected, "the clicked row must be the same element, not a rebuilt one");
  assert.deepEqual(p.$$("#pk-rows tr"), others, "no row was replaced");
  assert.equal(p.d.activeElement, cb, "focus stays on the checkbox");
  assert.equal(row.className, "off", "row shows as not selected");
  assert.match(row.textContent, /Apply and save/, "status cell updated");
  assert.notEqual(p.text("#pk-fit"), fitBefore, "totals updated");
  cb.checked = true; p.fire(cb, "change");
  assert.equal(row.className, "");
  assert.match(row.textContent, /Backed up/);
  assert.equal(p.text("#pk-fit"), fitBefore, "totals back to where they were");
});

test("selections: rebuilding the list (Check all, polling) keeps the scroll position", async () => {
  const p = await loadPage("selections");
  await openPicker(p);
  // jsdom has no layout, so emulate the browser: emptying the list snaps scrollTop to 0
  const scroller = p.$("#picker .modal .body");
  let top = 0;
  Object.defineProperty(scroller, "scrollTop", { get: () => top, set: (v) => { top = v; }, configurable: true });
  const tbody = p.$("#pk-rows");
  const realRemove = tbody.removeChild.bind(tbody);
  tbody.removeChild = (n) => { const r = realRemove(n); if (!tbody.firstChild) top = 0; return r; };
  scroller.scrollTop = 640;
  p.$("#pk-none").click();
  assert.equal(scroller.scrollTop, 640);
  p.$("#pk-all").click();
  assert.equal(scroller.scrollTop, 640);
});

test("selections: include mode with nothing checked is refused", async () => {
  const p = await loadPage("selections");
  await openPicker(p);
  p.$('#pk-mode button[data-mode="include"]').click();
  p.$("#pk-none").click();
  p.$("#pk-apply").click();
  await sleep(20);
  assert.equal(p.$("#picker").hidden, false);
  assert.match(p.text("#toast"), /at least one folder/);
});

test("selections: include mode keeps only checked folders", async () => {
  const p = await loadPage("selections");
  await openPicker(p);
  p.$('#pk-mode button[data-mode="include"]').click();
  p.$("#pk-none").click();
  const cb = pickerRow(p, "Small Show").querySelector("input");
  cb.checked = true; p.fire(cb, "change");
  p.$("#pk-root").checked = false; p.fire(p.$("#pk-root"), "change");
  p.$("#pk-apply").click();
  p.$("#save").click();
  await sleep(100);
  const b = p.sent.find((s) => s.method === "PUT").body.backups[0];
  assert.deepEqual([b.mode, b.folders, b.include_root_files], ["include", ["Small Show"], false]);
});

test("selections: cancel and discard leave the selection unchanged", async () => {
  const p = await loadPage("selections");
  await openPicker(p);
  uncheck(p, "Big Show");
  p.$("#pk-cancel").click();
  assert.equal(p.$$(".sel-summary")[0].textContent, "Folders: all");
  await openPicker(p);
  uncheck(p, "Big Show");
  p.$("#pk-apply").click();
  p.$("#revert").click();
  assert.equal(p.$$(".sel-summary")[0].textContent, "Folders: all");
  assert.equal(p.$("#save").disabled, true);
});

test("selections: filter and sort", async () => {
  const p = await loadPage("selections");
  await openPicker(p);
  p.$("#pk-filter").value = "small"; p.fire(p.$("#pk-filter"), "input");
  assert.deepEqual(p.$$("#pk-rows .name").map((e) => e.textContent), ["Small Show"]);
  p.$("#pk-filter").value = ""; p.fire(p.$("#pk-filter"), "input");
  p.$("#pk-sort").value = "size"; p.fire(p.$("#pk-sort"), "change");
  assert.equal(p.$$("#pk-rows .name")[0].textContent, "Big Show");
});

test("selections: no Remove button until the deselection is saved", async () => {
  const p = await loadPage("selections");
  await openPicker(p);
  uncheck(p, "Big Show");
  assert.equal(pickerRow(p, "Big Show").querySelector("button"), null);
  assert.match(pickerRow(p, "Big Show").textContent, /Apply and save/);
});

test("selections: removing a saved deselection needs the exact folder name", async () => {
  const api = JSON.parse(JSON.stringify(API));
  api["/api/config"].backups[0].mode = "exclude";
  api["/api/config"].backups[0].folders = ["Big Show"];
  api["/api/folders"].saved_selection = { mode: "exclude", folders: ["Big Show"], include_root_files: true };
  const p = await loadPage("selections", { api });
  assert.match(p.$$(".sel-summary")[0].textContent, /all except 1/);
  await openPicker(p);
  assert.equal(pickerRow(p, "Big Show").querySelector("input").checked, false);
  const btn = pickerRow(p, "Big Show").querySelector("button");
  assert.ok(btn && /Remove copy/.test(btn.textContent));
  btn.click();
  assert.equal(p.$("#pk-confirm").hidden, false);
  const input = p.$("#pkc-input");
  for (const wrong of ["", "big show", "Big Show ", "Big"]) {
    input.value = wrong; p.fire(input, "input");
    assert.equal(p.$("#pkc-go").disabled, true, `"${wrong}" must not enable delete`);
  }
  input.value = "Big Show"; p.fire(input, "input");
  assert.equal(p.$("#pkc-go").disabled, false);
  p.$("#pkc-go").click();
  await sleep(50);
  const req = p.sent.find((s) => s.path === "/api/remove-dest-folder");
  assert.deepEqual(req.body, { source: "/mnt/src/TV", destination: "/mnt/disk1", folder: "Big Show" });
  assert.match(pickerRow(p, "Big Show").textContent, /Removing/);
});

test("selections: orphans can be removed (also needs the name)", async () => {
  const p = await loadPage("selections");
  await openPicker(p);
  const btn = p.$("#pk-orphans button");
  assert.ok(btn);
  btn.click();
  assert.match(p.text("#pkc-text"), /Gone Show/);
  p.$("#pkc-cancel").click();
  assert.equal(p.$("#pk-confirm").hidden, true);
  assert.equal(p.sent.length, 0);
});

test("selections: validation errors from the server are shown and nothing is lost", async () => {
  const p = await loadPage("selections", {
    respond: (m) => (m === "PUT" ? { status: 422, body: { errors: ["Backup #1: source does not exist"], warnings: [] } } : null) });
  await openPicker(p);
  uncheck(p, "Big Show");
  p.$("#pk-apply").click();
  p.$("#save").click();
  await sleep(100);
  assert.match(p.text("#messages"), /source does not exist/);
  assert.equal(p.$("#save").disabled, false, "still dirty - edits kept");
});

test("selections: warnings offer Save anyway, which sends force", async () => {
  let calls = 0;
  const p = await loadPage("selections", {
    respond: (m, path, body) => {
      if (m !== "PUT") return null;
      calls++;
      return body.force ? { status: 200, body: { ok: true, warnings: [] } }
                        : { status: 422, body: { errors: [], warnings: ["rsync options include --delete"] } };
    } });
  p.$("#rsync-opts").value = "-a --delete"; p.fire(p.$("#rsync-opts"), "input");
  p.$("#save").click();
  await sleep(100);
  const btn = p.$$("#messages button").find((b) => b.textContent === "Save anyway");
  assert.ok(btn);
  btn.click();
  await sleep(100);
  assert.equal(calls, 2);
  assert.equal(p.sent.filter((s) => s.method === "PUT")[1].body.force, true);
});

test("selections: conflict tells the user to reload", async () => {
  const p = await loadPage("selections", {
    respond: (m) => (m === "PUT" ? { status: 409, body: { errors: ["config.yaml was changed by someone else"] } } : null) });
  p.$("#rsync-opts").value = "-a"; p.fire(p.$("#rsync-opts"), "input");
  p.$("#save").click();
  await sleep(100);
  assert.match(p.text("#messages"), /changed by someone else/);
});

test("selections: add a backup using the folder browser", async () => {
  const p = await loadPage("selections");
  p.$("#add-backup").click();
  assert.equal(p.$$(".sel-row").length, 3);
  const row = p.$$(".sel-row")[2];
  row.querySelector("button[title=Browse]").click();
  await sleep(100);
  assert.equal(p.$("#browser").hidden, false);
  assert.ok(p.$$("#br-list button").length > 0);
  p.$("#br-select").click();
  assert.equal(row.querySelector("input").value, "/mnt");
  assert.equal(p.$("#save").disabled, false);
});

/* ------------------------------------------------------------------ Backup Folder Cleanup */
const PLAN = {
  id: "c".repeat(32), source: "/mnt/src/TV", destination: "/mnt/disk1", dest_folder: "/mnt/disk1/TV",
  confirm_name: "TV", protected: ["Kept Show"], total_items: 3, total_size: 380, total_files: 3,
  backup_size: 10000, share_of_backup: 0.038, items_truncated: false,
  items: [{ path: "Big Show/s1/e3-old.mkv", type: "file", size: 123, files: 1 },
          { path: "Gone Show", type: "folder", size: 250, files: 1 },
          { path: '<img src=x onerror="window.__xss=2">', type: "file", size: 7, files: 1 }],
};
function cleanupResponder(plan, finalStatus, extra) {
  const calls = { plan: 0, run: 0, status: 0 };
  return {
    calls,
    respond: (m, p, body) => {
      if (extra) { const r = extra(m, p, body); if (r) return r; }
      if (p === "/api/cleanup/plan") { calls.plan++; return { status: 200, body: plan }; }
      if (p === "/api/cleanup/run") { calls.run++; return { status: 200, body: { ok: true, id: plan.id } }; }
      if (p.startsWith("/api/cleanup/")) {
        calls.status++;
        return { status: 200, body: finalStatus || { status: "done", deleted: 3, skipped: 0, freed: 380, message: "Deleted 3" } };
      }
      return null;
    },
  };
}
const cleanupBtn = (p, i = 0) => p.$$(".folders-line button").filter((b) => b.textContent === "Backup Folder Cleanup")[i];

test("cleanup: one button per saved backup set, disabled while that set has unsaved changes", async () => {
  const p = await loadPage("selections");
  assert.equal(p.$$(".folders-line button").filter((b) => b.textContent === "Backup Folder Cleanup").length, 2);
  assert.equal(cleanupBtn(p, 0).disabled, false);
  const src = p.$$(".sel-row")[0].querySelector("input");
  src.value = "/mnt/src/Other"; p.fire(src, "input");
  assert.equal(cleanupBtn(p, 0).disabled, true, "edited path");
  assert.equal(cleanupBtn(p, 1).disabled, false, "other set unaffected");
  p.$("#revert").click();
  await openPicker(p, 0);
  uncheck(p, "Big Show");
  p.$("#pk-apply").click();
  assert.equal(cleanupBtn(p, 0).disabled, true, "changed selection");
  p.$("#add-backup").click();
  assert.equal(p.$$(".folders-line button").filter((b) => b.textContent === "Backup Folder Cleanup").pop().disabled, true, "new set");
});

test("cleanup: shows what would be deleted; Delete needs the exact folder name", async () => {
  const r = cleanupResponder(PLAN);
  const p = await loadPage("selections", { respond: r.respond });
  cleanupBtn(p).click();
  await sleep(100);
  assert.equal(p.$("#cleanup").hidden, false);
  assert.equal(r.calls.plan, 1);
  assert.equal(p.sent[0].path, "/api/cleanup/plan");
  assert.deepEqual(p.sent[0].body, { source: "/mnt/src/TV", destination: "/mnt/disk1" });
  assert.match(p.text("#cu-body"), /Items to delete ?3/);
  assert.match(p.text("#cu-body"), /Gone Show\//);
  assert.match(p.text("#cu-body"), /Kept Show/, "protected folders are named");
  assert.equal(p.w.__xss, undefined, "paths are text, never HTML");
  assert.equal(p.$("#cu-confirm").hidden, false);
  assert.equal(p.text("#cu-name"), "TV");
  assert.match(p.$("#cu-go").textContent, /Delete 3 items/);
  const input = p.$("#cu-input");
  for (const wrong of ["", "tv", "TV ", "T", "TV-Shows"]) {
    input.value = wrong; p.fire(input, "input");
    assert.equal(p.$("#cu-go").disabled, true, `"${wrong}" must not enable delete`);
  }
  assert.equal(r.calls.run, 0);
  input.value = "TV"; p.fire(input, "input");
  assert.equal(p.$("#cu-go").disabled, false);
  p.$("#cu-go").click();
  await sleep(150);
  const run = p.sent.find((s) => s.path === "/api/cleanup/run");
  assert.deepEqual(run.body, { plan_id: PLAN.id, confirm: "TV" });
  assert.match(p.text("#cu-body"), /Cleanup finished/);
  assert.match(p.text("#cu-body"), /Deleted 3 items, freed 380 B/);
});

test("cleanup: closing before confirming deletes nothing", async () => {
  const r = cleanupResponder(PLAN);
  const p = await loadPage("selections", { respond: r.respond });
  cleanupBtn(p).click();
  await sleep(100);
  p.$("#cu-close").click();
  assert.equal(p.$("#cleanup").hidden, true);
  assert.equal(r.calls.run, 0);
});

test("cleanup: large share of the backup gets a prominent warning", async () => {
  const big = Object.assign({}, PLAN, { share_of_backup: 0.6 });
  const p = await loadPage("selections", { respond: cleanupResponder(big).respond });
  cleanupBtn(p).click();
  await sleep(100);
  assert.match(p.text("#cu-body"), /delete 60% of the backup/);
});

test("cleanup: in-sync backup says so and offers no Delete", async () => {
  const none = Object.assign({}, PLAN, { items: [], total_items: 0, total_size: 0, total_files: 0 });
  const p = await loadPage("selections", { respond: cleanupResponder(none).respond });
  cleanupBtn(p).click();
  await sleep(100);
  assert.match(p.text("#cu-body"), /Nothing to clean up/);
  assert.equal(p.$("#cu-confirm").hidden, true);
});

test("cleanup: refusal from the comparison is shown, no Delete", async () => {
  const p = await loadPage("selections", { respond: (m, path) => path === "/api/cleanup/plan"
    ? { status: 409, body: { error: "The source folder /mnt/src/TV is empty." } } : null });
  cleanupBtn(p).click();
  await sleep(100);
  assert.match(p.text("#cu-body"), /is empty/);
  assert.equal(p.$("#cu-confirm").hidden, true);
});

test("cleanup: host refusal and skipped items are reported", async () => {
  const st = { status: "done", deleted: 2, skipped: 1, freed: 257, message: "x",
               skipped_items: [{ path: "Gone Show", reason: "exists in the source again" }] };
  const p = await loadPage("selections", { respond: cleanupResponder(PLAN, st).respond });
  cleanupBtn(p).click();
  await sleep(100);
  p.$("#cu-input").value = "TV"; p.fire(p.$("#cu-input"), "input");
  p.$("#cu-go").click();
  await sleep(150);
  assert.match(p.text("#cu-body"), /1 item\(s\) were skipped and kept/);
  assert.match(p.text("#cu-body"), /exists in the source again/);

  const p2 = await loadPage("selections", { respond: cleanupResponder(PLAN,
    { status: "failed", message: "source folder /mnt/src/TV is empty - is its disk mounted?", deleted: 0, skipped: 0 }).respond });
  cleanupBtn(p2).click();
  await sleep(100);
  p2.$("#cu-input").value = "TV"; p2.fire(p2.$("#cu-input"), "input");
  p2.$("#cu-go").click();
  await sleep(150);
  assert.match(p2.text("#cu-body"), /Cleanup refused/);
  assert.match(p2.text("#cu-body"), /is its disk mounted/);
});

/* ------------------------------------------------------------------ Schedule card */
function scheduleResponder({ previewBody, previewStatus = 200, hostStatus = "applied", hostMessage = 'set to "0 2 * * *" by gozz' } = {}) {
  let savedId = null;
  const saved = JSON.parse(JSON.stringify(API["/api/schedule"]));
  return (m, p) => {
    if (p === "/api/schedule/preview") {
      return { status: previewStatus, body: previewBody || API["POST /api/schedule/preview"] };
    }
    if (p === "/api/schedule" && m === "PUT") { savedId = "id-1"; return { status: 200, body: { ok: true, id: savedId, expr: "0 2 * * *" } }; }
    if (p === "/api/schedule" && m === "GET" && savedId) {
      return { status: 200, body: Object.assign({}, saved, { pending: false, managed: true, enabled: hostStatus === "applied",
        expr: "0 2 * * *", unmanaged: [], last_status: { id: savedId, status: hostStatus, message: hostMessage } }) };
    }
    return null;
  };
}

test("schedule: shows that automatic backups are off and lists the old cron line", async () => {
  const p = await loadPage("selections");
  assert.equal(p.$("#sch-enabled").checked, false);
  assert.match(p.text("#sch-notes"), /Automatic backups are off/);
  assert.match(p.text("#sch-notes"), /comments out these lines/);
  assert.match(p.text("#sch-notes"), /backup\.sh/);
  assert.equal(p.$("#sch-kind").value, "daily");
  assert.match(p.text("#sch-preview"), /Off\./);
});

test("schedule: the form shows the fields for each kind of schedule", async () => {
  const p = await loadPage("selections");
  const shown = () => ["#sch-time-wrap", "#sch-days", "#sch-every-wrap", "#sch-expr-wrap"].filter((s) => !p.$(s).hidden);
  const set = (k) => { p.$("#sch-kind").value = k; p.fire(p.$("#sch-kind"), "change"); };
  set("daily"); assert.deepEqual(shown(), ["#sch-time-wrap"]);
  set("weekdays"); assert.deepEqual(shown(), ["#sch-time-wrap", "#sch-days"]);
  assert.equal(p.$$("#sch-days input").length, 7);
  set("hours"); assert.deepEqual(shown(), ["#sch-every-wrap"]);
  set("advanced"); assert.deepEqual(shown(), ["#sch-expr-wrap"]);
});

test("schedule: turning it on previews the next runs and saves to the host", async () => {
  const p = await loadPage("selections", { respond: scheduleResponder() });
  p.$("#sch-enabled").checked = true; p.fire(p.$("#sch-enabled"), "change");
  await sleep(400);
  assert.match(p.text("#sch-preview"), /Daily at 02:00/);
  assert.match(p.text("#sch-preview"), /Next runs:/);
  assert.match(p.text("#sch-preview"), /your time/);
  const pv = p.previews[p.previews.length - 1].body;
  assert.equal(pv.enabled, true);
  assert.equal(pv.kind, "daily");
  assert.equal(pv.time, "02:00");
  assert.equal(p.$("#sch-save").disabled, false);
  p.$("#sch-save").click();
  await sleep(100);
  const put = p.sent.find((s) => s.method === "PUT" && s.path === "/api/schedule");
  assert.ok(put, "PUT /api/schedule sent");
  assert.equal(put.body.enabled, true);
  assert.equal(put.body.kind, "daily");
  assert.ok(put.csrf, "carries the CSRF token");
  assert.match(p.text("#sch-status"), /Applying|Saved/);
  await sleep(1700);
  assert.match(p.text("#sch-status"), /✓ Saved/);
  assert.equal(p.sent.filter((s) => s.path === "/api/config").length, 0, "does not touch config.yaml");
});

test("schedule: an invalid custom schedule is explained and cannot be saved", async () => {
  const p = await loadPage("selections", { respond: scheduleResponder({ previewStatus: 422,
    previewBody: { errors: ["The minute field '61' is outside 0-59"] } }) });
  p.$("#sch-kind").value = "advanced"; p.fire(p.$("#sch-kind"), "change");
  p.$("#sch-expr").value = "61 2 * * *"; p.fire(p.$("#sch-expr"), "input");
  await sleep(400);
  assert.match(p.text("#sch-preview"), /outside 0-59/);
  assert.equal(p.$("#sch-save").disabled, true);
});

test("schedule: a refusal from the host is shown", async () => {
  const p = await loadPage("selections", { respond: scheduleResponder({ hostStatus: "failed",
    hostMessage: "refused: crontab rejected the change" }) });
  p.$("#sch-enabled").checked = true; p.fire(p.$("#sch-enabled"), "change");
  await sleep(400);
  p.$("#sch-save").click();
  await sleep(1900);
  assert.match(p.text("#sch-preview"), /Not saved: refused: crontab rejected the change/);
  assert.doesNotMatch(p.text("#sch-status"), /Saved/);
});

test("schedule: an existing managed schedule opens filled in, and Save stays off until something changes", async () => {
  const api = JSON.parse(JSON.stringify(API));
  Object.assign(api["/api/schedule"], { managed: true, enabled: true, expr: "15 3 * * 1,3,5", unmanaged: [],
    form: { kind: "weekdays", time: "03:15", days: [1, 3, 5], text: "Mon, Wed, Fri at 03:15" } });
  const pv = { expr: "15 3 * * 1,3,5", form: api["/api/schedule"].form, next_runs: [Date.now() / 1000 + 3600] };
  const p = await loadPage("selections", { api, respond: scheduleResponder({ previewBody: pv }) });
  assert.equal(p.$("#sch-enabled").checked, true);
  assert.equal(p.$("#sch-kind").value, "weekdays");
  assert.equal(p.$("#sch-time").value, "03:15");
  assert.deepEqual(p.$$("#sch-days input:checked").map((c) => c.value).sort(), ["1", "3", "5"]);
  assert.equal(p.text("#sch-notes"), "");
  assert.equal(p.$("#sch-save").disabled, true, "nothing changed yet");
});

/* ------------------------------------------------------------------ runner */
(async () => {
  let failed = 0;
  for (const t of tests) {
    try {
      await t.fn();
      console.log("  ok    " + t.name);
    } catch (e) {
      failed++;
      console.log("  FAIL  " + t.name + "\n        " + (e.message || e).split("\n").join("\n        "));
    }
  }
  console.log(`${tests.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
