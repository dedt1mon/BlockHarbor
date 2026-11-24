// Exercise the production catalog controller with delayed preparation/acceptance.
// Browser geometry stays in the existing focused release smoke flow.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");

const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function element(dataset = {}) {
  const classes = new Set();
  return {dataset, isConnected: true, disabled: false, textContent: "", attributes: {},
    classList: {add: name => classes.add(name), remove: name => classes.delete(name)},
    setAttribute(name, value) {this.attributes[name] = value;},
    matches: () => false, closest() {return this;}, focus() {}};
}
function harness() {
  const listeners = {}, closeListeners = [], previews = [], jobs = [], discards = [], notices = [], modals = [];
  let host = null, prepare = null, closes = 0;
  const dialog = {open: false, addEventListener: (type, fn) => closeListeners.push(fn)};
  function close() {
    closes++;
    dialog.open = false;
    closeListeners.forEach(fn => fn());
  }
  const context = {
    URLSearchParams, AbortController, Intl,
    window: {},
    document: {
      body: {}, activeElement: null,
      addEventListener: (type, fn) => {listeners[type] = fn;},
      querySelector(selector) {
        if (selector === "#dialog") return dialog;
        if ([".pack-release", "#dialog .pack-release"].includes(selector)) return host;
        if (selector === "#dialogHeaderActions [data-pack-prepare]") return prepare;
        return null;
      },
    },
    modal(title) {
      if (host) host.isConnected = false;
      modals.push(title);
      const select = element({packVersion: "Project"});
      const feedback = element(), steam = element();
      host = {...element(), select, feedback, steam, querySelector(selector) {
        if (["select", "[data-pack-version]"].includes(selector)) return select;
        if (selector === "[data-pack-install-status]") return feedback;
        if (selector === "[data-pack-steam-note]") return steam;
        return null;
      }};
      prepare = element({packPrepare: ""});
      dialog.open = true;
    },
    closeDialog: close,
    api: async () => ({project: "Project", title: "Catalog pack", description: "Details", versions: [
      {id: "Release", type: "release", minecraft: ["1.16.5"], loaders: ["fabric"], steam: {supported: false}},
    ]}),
    act(action, data) {
      if (action === "packPreview") {
        const pending = deferred(); previews.push({...pending, data}); return pending.promise;
      }
      assert.equal(action, "packDiscard");
      discards.push(data.token);
      return Promise.resolve({ok: true});
    },
    runJob(action, data) {
      assert.equal(action, "packInstall");
      const pending = deferred(); jobs.push({...pending, data}); return pending.promise;
    },
    confirmSteamCompatibility() {throw new Error("Unexpected confirmation dialog");},
    esc: value => String(value ?? ""), ic: () => "", loaders: {fabric: "Fabric"},
    notice: message => notices.push(message),
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "ui", "pack-hub.js"), "utf8"), context);
  const click = target => listeners.click({target});
  return {
    async open() {await click(element({packProject: "Project"}));},
    install: () => click(prepare), close,
    replace() {host.isConnected = false; prepare.isConnected = false; host = null; prepare = null; dialog.open = true;},
    get host() {return host;}, get prepare() {return prepare;}, get closes() {return closes;},
    previews, jobs, discards, notices, modals, dialog,
  };
}

test("one catalog click prepares and installs without another dialog or duplicate request", async () => {
  const h = harness();
  await h.open();
  assert.match(h.host.steam.textContent, /без него/);
  const pending = h.install();
  await h.install();
  assert.equal(h.previews.length, 1);
  assert.equal(h.jobs.length, 0, "Installation ran before the archive was verified");
  assert.equal(h.host.select.disabled, true);
  h.previews[0].resolve({token: "checked-archive", name: "Author pack"});
  await flush();
  assert.equal(h.jobs.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(h.jobs[0].data)), {
    token: "checked-archive", name: "Author pack", allowWithoutSteam: true, discardOnFinish: true,
  }, "Backend defaults must own the optional component selection");
  assert.deepEqual(h.discards, [], "A token awaiting job acceptance was discarded");
  h.jobs[0].resolve("accepted-job");
  await pending;
  assert.deepEqual(h.modals, ["Выбрать выпуск"]);
  assert.deepEqual(h.notices, []);
  assert.deepEqual(h.discards, []);
  assert.equal(h.dialog.open, false);
});

test("a closed or replaced release selector discards its late preview without installing", async () => {
  for (const leave of ["close", "replace"]) {
    const h = harness();
    await h.open();
    const pending = h.install();
    h[leave]();
    h.previews[0].resolve({token: "abandoned-archive", name: "Author pack"});
    await pending;
    assert.equal(h.jobs.length, 0);
    assert.deepEqual(h.discards, ["abandoned-archive"]);
    assert.deepEqual(h.notices, []);
  }
});

test("a pending install keeps its token and never closes a replacement dialog", async () => {
  const h = harness();
  await h.open();
  const pending = h.install();
  h.previews[0].resolve({token: "installing-archive", name: "Author pack"});
  await flush();
  h.close();
  h.replace();
  assert.deepEqual(h.discards, []);
  h.jobs[0].resolve("accepted-job");
  await pending;
  assert.deepEqual(h.discards, []);
  assert.equal(h.dialog.open, true);
  assert.equal(h.closes, 1);
});

test("rejected installation releases the staged archive and leaves an inline retryable error", async () => {
  const h = harness();
  await h.open();
  const pending = h.install();
  h.previews[0].resolve({token: "rejected-archive", name: "Author pack"});
  await flush();
  h.jobs[0].reject(new Error("Another operation is running"));
  await pending;
  assert.deepEqual(h.discards, ["rejected-archive"]);
  assert.equal(h.dialog.open, true);
  assert.equal(h.prepare.disabled, false);
  assert.equal(h.host.select.disabled, false);
  assert.equal(h.host.feedback.textContent, "Another operation is running");
  const retry = h.install();
  assert.equal(h.previews.length, 2);
  h.previews[1].reject(new Error("Download failed"));
  await retry;
  assert.deepEqual(h.discards, ["rejected-archive"]);
});
