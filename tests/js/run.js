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
  const sent = [];
  const data = JSON.parse(JSON.stringify(api));
  w.fetch = async (url, opts = {}) => {
    const u = new URL(url, "https://portal.test");
    const method = opts.method || "GET";
    let p = u.pathname.startsWith("/api/history/run/") ? "/api/history/run" : u.pathname;
    if (method !== "GET") sent.push({ method, path: u.pathname, body: opts.body ? JSON.parse(opts.body) : null,
                                      csrf: opts.headers && opts.headers["X-CSRF-Token"] });
    let status = 200, body;
    const custom = respond && respond(method, p, opts.body ? JSON.parse(opts.body) : null);
    if (custom) ({ status, body } = custom);
    else if (method === "GET") body = data[p];
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
  return { w, d, $, $$, text, fire, sent, errors, data };
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
  assert.match(p.text("#status-flags"), /No backup.sh entry found/);
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
