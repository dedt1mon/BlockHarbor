// Exercise the catalog's real controller after a failed network read.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness({failFirst = false} = {}) {
  const listeners = {}, requests = [];
  let moment = 100000, open = true;
  const pager = {innerHTML: ""};
  const results = {isConnected: true, innerHTML: "", closest: () => ({querySelector: () => pager})};
  const dialog = {open: true, addEventListener() {}};
  const context = {
    AbortController, URLSearchParams, Intl,
    Date: class extends Date {static now() {return moment;}},
    window: {},
    state: {profiles: [], versions: []},
    document: {
      hidden: false,
      addEventListener(type, callback) {listeners[type] = callback;},
      querySelector(selector) {
        if (selector === "#dialog") return dialog;
        if (selector === '.pack-dialog[data-pack-view="catalog"]') return open ? {} : null;
        if (selector === ".community-results") return results;
        return null;
      },
    },
    async api(url) {
      const params = new URLSearchParams(url.split("?")[1]);
      requests.push(Object.fromEntries(params));
      if (failFirst && requests.length === 1) throw new Error("Network unavailable");
      return {hits: [], total: 0, limit: 20, offset: 0, source: "primary"};
    },
    FormData: class {constructor(form) {this.form = form;} get() {return this.form.q;}},
    esc: value => String(value ?? ""), ic: () => "", fmt: String, loaders: {},
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "ui", "pack-hub.js"), "utf8"), context);
  return {
    requests, results,
    observe: () => context.window.PackHubUI.observeJob({}),
    advance: ms => {moment += ms;},
    hide: () => {context.document.hidden = true;},
    show: () => {context.document.hidden = false;},
    close: () => {open = false;},
    search: q => listeners.submit({target: {q, matches: selector => selector === "[data-pack-search]"}, preventDefault() {}}),
  };
}

test("a failed community search recovers automatically without flooding the server", async () => {
  const h = harness({failFirst: true});
  h.observe();
  await flush();
  assert.equal(h.requests.length, 1);
  assert.match(h.results.innerHTML, /Network unavailable/);
  for (let second = 0; second < 14; second++) {h.advance(1000); h.observe();}
  await flush();
  assert.equal(h.requests.length, 1);
  h.advance(1000);
  h.observe();
  await flush();
  assert.equal(h.requests.length, 2);
  assert.doesNotMatch(h.results.innerHTML, /Network unavailable/);
  h.hide();
  h.advance(60000);
  h.observe();
  await flush();
  assert.equal(h.requests.length, 2, "A hidden catalog must not poll for new publications");
  h.show();
  h.close();
  h.observe();
  await flush();
  assert.equal(h.requests.length, 2, "A closed catalog must not poll for new publications");
});

test("a copied public archive link searches only the community registry", async () => {
  const h = harness();
  const url = "https://blockharbor-community-catalog.ded8timon.chatgpt.site/v1/builds/BH-ABCDEF123456/archive";
  h.search(url);
  await flush();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].action, "communitySearch");
  assert.equal(h.requests[0].q, url);
});
