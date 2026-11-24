// Exercise the catalog's real controller after a failed network read.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness({failFirst = false, communitySharing} = {}) {
  const listeners = {}, requests = [];
  let moment = 100000, open = true;
  const pager = {innerHTML: ""};
  const results = {isConnected: true, innerHTML: "", closest: () => ({querySelector: () => pager})};
  const dialog = {open: true, addEventListener() {}};
  const context = {
    AbortController, URLSearchParams, Intl,
    Date: class extends Date {static now() {return moment;}},
    window: {},
    state: {profiles: [], versions: [], capabilities: {communitySharing}},
    document: {
      hidden: false,
      addEventListener(type, callback) {listeners[type] = callback;},
      querySelectorAll() {return [];},
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
    modal() {throw new Error("Unexpected publication dialog");},
    runJob() {throw new Error("Unexpected community action");},
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
    mount: () => context.window.PackHubUI.mount(),
    render: () => context.window.PackHubUI.render(),
    click: dataset => listeners.click({target: {closest: () => ({dataset})}}),
    publish: () => listeners.submit({target: {matches: selector => selector === "[data-pack-publish]"}, preventDefault() {}}),
    search: q => listeners.submit({target: {q, matches: selector => selector === "[data-pack-search]"}, preventDefault() {}}),
  };
}

test("a failed community search recovers automatically without flooding the server", async () => {
  const h = harness({failFirst: true, communitySharing: true});
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
  const h = harness({communitySharing: true});
  const url = "https://blockharbor-community-catalog.ded8timon.chatgpt.site/v1/builds/BH-ABCDEF123456/archive";
  h.search(url);
  await flush();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].action, "communitySearch");
  assert.equal(h.requests[0].q, url);
});

test("disabled or absent sharing capability makes stale controls inert and retains Modrinth search", async () => {
  for (const communitySharing of [false, undefined]) {
    const h = harness({communitySharing});
    const markup = h.render();
    assert.doesNotMatch(markup, /community-results|community-page-host|data-pack-community|ID или ссылка/);
    assert.match(markup, /placeholder="Название модпака"/);
    h.mount();
    h.observe();
    h.advance(60000);
    h.observe();
    for (const key of ["packShare", "packWithdraw", "packPublicationRetry", "packPolicyRetry", "packCopyId", "packCopyLink", "packCommunity", "packCommunityPage", "packCommunityRetry"])
      await h.click({[key]: "BH-ABCDEF123456"});
    h.publish();
    await flush();
    assert.equal(h.requests.length, 0, "Disabled sharing must not read or mutate the registry");
    const queries = ["BH-ABCDEF123456", "https://blockharbor-community-catalog.ded8timon.chatgpt.site/v1/builds/BH-ABCDEF123456/archive"];
    for (const q of queries) {h.search(q); await flush();}
    assert.deepEqual(h.requests.map(row => row.action), ["packSearch", "packSearch"]);
    assert.deepEqual(h.requests.map(row => row.q), queries);
    h.observe();
    assert.equal(h.requests.length, 2);
  }
});
