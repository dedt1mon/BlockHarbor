/* Run with node tests/window-lifecycle.cjs. No browser or npm packages required. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const source = fs.readFileSync(path.join(__dirname, "../ui/window.js"), "utf8");
const token = "window-session-token-from-launcher";

function page({ marker = "", beacon = true, stored = false } = {}) {
  const events = new Map(), calls = [], sent = [];
  const values = new Map(stored ? [["blockharbor-key", token]] : []);
  const window = { addEventListener(name, callback) { events.set(name, [...(events.get(name) || []), callback]); } };
  const context = { window, document: { title: "BlockHarbor", readyState: "loading" },
    location: { search: marker ? "?window=" + marker : "", hash: stored ? "" : "#" + token },
    sessionStorage: { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) },
    crypto: webcrypto, Uint8Array, URLSearchParams, Blob, setTimeout, clearTimeout,
    navigator: { sendBeacon(url, body) { sent.push({ url, body }); return beacon; } },
    fetch(url, options) { calls.push({ url, ...options }); return Promise.resolve({ ok: true, json: async () => ({ attached: true }) }); },
  };
  vm.runInNewContext(source, context, { filename: "ui/window.js" });
  return { calls, sent, context, fire(name, event = {}) { for (const callback of events.get(name) || []) callback(event); } };
}

(async () => {
  const browser = page();
  browser.fire("load");
  await new Promise(setImmediate);
  const opened = JSON.parse(browser.calls[0].body);
  assert.equal(browser.calls[0].url, "/api/window");
  assert.equal(opened.event, "opened");
  assert.equal(opened.token, token);
  assert.match(opened.page, /^[a-f0-9]{24}$/);
  assert.equal(browser.calls.filter(call => call.url === "/api/action").length, 0);
  browser.fire("visibilitychange");
  assert.equal(browser.sent.length, 0, "Minimizing or hiding must not close the session");
  browser.fire("pagehide");
  assert.equal(browser.sent.length, 1);
  assert.equal(browser.sent[0].url, "/api/window");
  assert.deepEqual(JSON.parse(await browser.sent[0].body.text()), { ...opened, event: "closed" });

  const reload = page({ stored: true });
  reload.fire("load");
  const reopened = JSON.parse(reload.calls[0].body);
  assert.equal(reopened.token, token);
  assert.notEqual(reopened.page, opened.page, "A reload must own a new page lease");

  const fallback = page({ beacon: false });
  fallback.fire("load");
  fallback.fire("pagehide");
  assert.equal(fallback.calls.at(-1).keepalive, true);
  assert.equal(JSON.parse(fallback.calls.at(-1).body).event, "closed");
  fallback.fire("pageshow", { persisted: true });
  assert.equal(JSON.parse(fallback.calls.at(-1).body).event, "opened");

  const native = page({ marker: "a".repeat(24) });
  native.fire("load");
  await new Promise(setImmediate);
  assert.equal(native.context.document.title, "BlockHarbor");
  assert.equal(native.calls.filter(call => call.url === "/api/window").length, 1);
  assert.deepEqual(JSON.parse(native.calls.find(call => call.url === "/api/action").body), { action: "fitWindow", marker: "a".repeat(24) });
  console.log("PASS: browser close, hidden window, reload, beacon fallback, page restore, native registration");
})().catch(error => { console.error(error); process.exitCode = 1; });
