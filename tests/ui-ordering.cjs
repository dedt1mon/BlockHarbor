// Reproduce asynchronous UI responses with the real production scripts.
// Geometry and click accessibility remain the responsibility of the Chromium suite.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");

const source = name => fs.readFileSync(path.join(__dirname, "..", "ui", name), "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
const response = data => ({ok: true, json: async () => data});

function appHarness({realRefresh = false} = {}) {
  const polls = [], states = [], started = [], observed = [], unread = [], notices = [];
  const dummy = {addEventListener() {}, classList: {toggle() {}}, querySelector() { return dummy; }};
  const context = {
    document: {hidden: false, querySelector: () => dummy, querySelectorAll: () => [], addEventListener() {}},
    window: {addEventListener() {}, scrollTo() {}, BatchUI: {start: id => started.push(id), observe: job => observed.push(job.id)}, ExperienceUI: {observeNotifications: value => unread.push(value)}},
    location: {hash: "#test-token", pathname: "/"},
    sessionStorage: {setItem() {}, getItem: () => "test-token"}, history: {replaceState() {}},
    setTimeout: () => 1, clearTimeout() {}, URLSearchParams, AbortController,
    fetch(url, options) {
      assert.equal(options.headers.Authorization, "Bearer test-token");
      if (url === "/api/state") {
        // The first request belongs to startup and remains independent of the
        // explicitly controlled state refreshes in the regression scenarios.
        const pending = deferred(); states.push(pending); return pending.promise;
      }
      if (url === "/api/action") return Promise.resolve(response({job: "new-job"}));
      assert.equal(url, "/api/job");
      const pending = deferred(); polls.push(pending); return pending.promise;
    },
    refreshed: 0,
    rendered: 0,
    recordNotice: message => notices.push(message),
  };
  vm.createContext(context);
  vm.runInContext(source("app.js"), context, {filename: "ui/app.js"});
  vm.runInContext(`
    state = {profiles: [], job: {id: 'initial', busy: false}};
    notice = message => recordNotice(message);
    backgroundNotice = () => {};
    updatePlayButtons = () => {};
    showGameExit = () => {};
    showStagedReview = () => {};
    render = () => { rendered++; };
    ${realRefresh ? "" : "refresh = async () => { refreshed++; };"}
  `, context);
  return {context, polls, states, started, observed, unread, notices, run: code => vm.runInContext(code, context)};
}

for (const busy of [false, true]) {
  test(`an old ${busy ? "running" : "completed"} poll cannot replace a newly accepted job`, async () => {
    const h = appHarness();
    const oldPoll = h.run("tick(true)");
    assert.equal(h.polls.length, 1);
    await h.run("runJob('installModrinth', {id:'profile',items:[{project:'new-project',kind:'mods'}]})");
    assert.deepEqual(h.started, ["new-job"]);
    h.polls[0].resolve(response({id: "old-job", busy, ok: true, message: "Old result", notificationUnread: 0}));
    await oldPoll;
    assert.equal(h.run("state.job.id"), "initial", "Stale poll replaced global state.job");
    assert.equal(h.run("lastJob"), "new-job", "Stale poll replaced the accepted job id");
    assert.deepEqual(h.observed, [], "Stale poll reached the batch component");
    assert.deepEqual(h.unread, [], "Stale job response also replaced notification state");
    assert.equal(h.context.refreshed, 0);
    const nextPoll = h.run("tick(true)");
    h.polls[1].resolve(response({id: "new-job", busy: false, ok: true, message: "Installed", notificationUnread: 1}));
    await nextPoll;
    assert.equal(h.run("state.job.id"), "new-job");
    assert.equal(h.context.refreshed, 1, "Current completion did not refresh installed content");
    assert.deepEqual(h.observed, ["new-job"]);
    assert.deepEqual(h.unread, [1]);
  });
}

test("a completion paused in its state refresh cannot publish over a newly accepted job", async () => {
  const h = appHarness({realRefresh: true});
  h.run("lastJob = 'old-job'");
  const oldPoll = h.run("tick(true)");
  h.polls[0].resolve(response({id: "old-job", busy: false, ok: true, message: "Old result", notificationUnread: 0}));
  await flush();
  assert.equal(h.states.length, 2, "Completion did not enter the delayed state refresh");
  await h.run("runJob('installModrinth', {id:'profile',items:[{project:'new-project'}]})");
  h.states[1].resolve(response({profiles: [], job: {id: "old-job", busy: false}, nickname: "Stale profile", notificationUnread: 0}));
  await oldPoll;
  assert.equal(h.run("lastJob"), "new-job");
  assert.equal(h.run("state.nickname"), undefined, "Stale full state replaced the current profile");
  assert.equal(h.context.rendered, 0, "Stale state refresh rerendered the new operation");
  assert.deepEqual(h.observed, [], "Old completion reached observers after its delayed refresh");
  assert.deepEqual(h.unread, []);
  const currentPoll = h.run("tick(true)");
  h.polls[1].resolve(response({id: "new-job", busy: false, ok: true, message: "Installed", notificationUnread: 1}));
  await flush();
  h.states[2].resolve(response({profiles: [], job: {id: "new-job", busy: false}, nickname: "Current profile", notificationUnread: 1}));
  await currentPoll;
  assert.equal(h.run("state.nickname"), "Current profile");
  assert.equal(h.context.rendered, 1);
  assert.deepEqual(h.observed, ["new-job"]);
  assert.deepEqual(h.unread, [1]);
});

test("an obsolete poll error cannot replace the new job's notice", async () => {
  const h = appHarness();
  const oldPoll = h.run("tick(true)");
  await h.run("runJob('installModrinth', {id:'profile',items:[{project:'new-project'}]})");
  const currentNotices = [...h.notices];
  h.polls[0].reject(new Error("Old request failed"));
  await oldPoll;
  assert.deepEqual(h.notices, currentNotices);
  assert.equal(h.run("pollError"), "");
  assert.equal(h.run("lastJob"), "new-job");
});

for (const outcome of ["success", "error"]) {
  test(`a navigation ${outcome} captured before a new job preserves that job and the requested destination`, async () => {
    const h = appHarness({realRefresh: true});
    const navigation = h.run("navigateFresh('settings', 'downloads')");
    const accepted = h.run("runJob('installModrinth', {id:'profile',items:[{project:'new-project'}]})");
    await flush();
    h.polls[0].resolve(response({id: "new-job", busy: true, message: "Installing", notificationUnread: 2}));
    await accepted;
    const currentNotices = [...h.notices];
    if (outcome === "success") h.states[1].resolve(response({profiles: [], job: {id: "old-job"}, nickname: "Stale profile", notificationUnread: 0}));
    else h.states[1].reject(new Error("Old navigation request failed"));
    await navigation;
    assert.equal(h.run("state.job.id"), "new-job");
    assert.equal(h.run("state.nickname"), undefined);
    assert.deepEqual(h.unread, [2]);
    assert.deepEqual(h.notices, currentNotices);
    assert.equal(h.run("view"), "settings");
    assert.equal(h.run("settingsSection"), "downloads");
    assert.equal(h.context.rendered, 1, "The current destination was not rendered from the retained state");
  });
}

test("a current navigation failure still reaches its error handler", async () => {
  const h = appHarness({realRefresh: true});
  const navigation = h.run("navigateFresh('settings')");
  h.states[1].reject(new Error("Current request failed"));
  await assert.rejects(navigation, /Связь с приложением прервана/);
  assert.equal(h.run("view"), "home");
  assert.equal(h.context.rendered, 0);
});

for (const [older, newer] of [["refresh()", "refresh()"], ["navigateFresh('settings')", "refresh()"], ["refresh()", "navigateFresh('settings')"]]) {
  test(`a newer ${newer} survives an out-of-order ${older} response in the same job generation`, async () => {
    const h = appHarness({realRefresh: true});
    const previous = h.run(older);
    const current = h.run(newer);
    h.states[2].resolve(response({profiles: [], job: {id: "current-job"}, nickname: "Current profile", notificationUnread: 2}));
    await current;
    h.states[1].resolve(response({profiles: [], job: {id: "previous-job"}, nickname: "Stale profile", notificationUnread: 0}));
    await previous;
    assert.equal(h.run("state.job.id"), "current-job");
    assert.equal(h.run("state.nickname"), "Current profile");
    assert.equal(h.run("state.notificationUnread"), 2);
    assert.equal(h.context.rendered, older === "refresh()" ? 1 : 2);
    if (older !== "refresh()") assert.equal(h.run("view"), "settings");
  });
}

test("batch component waits for the id returned by its own start request", () => {
  const toggle = {hidden: true, title: "", setAttribute() {}, classList: {toggle(_name, value) { toggle.active = value; }}};
  const context = {window: {}, state: {profiles: []}, document: {addEventListener() {}, querySelector: selector => selector === "[data-batch-open]" ? toggle : null}, modal() {}, loaders: {}};
  vm.createContext(context);
  vm.runInContext(source("batch-install.js"), context, {filename: "ui/batch-install.js"});
  const batch = context.window.BatchUI;
  batch.start("new-job", {id: "profile", items: [{project: "new-project", kind: "mods"}]});
  assert.equal(toggle.active, true);
  batch.observe({id: "old-job", kind: "catalog-install", busy: false, items: [{project: "old-project", status: "error"}]});
  assert.equal(toggle.active, true, "Old completion replaced the new operation");
  batch.observe({id: "new-job", kind: "catalog-install", busy: false, items: [{project: "new-project", status: "installed"}]});
  assert.equal(toggle.active, false, "Matching completion was never accepted");
});

test("a queued close from the previous modal does not cancel the newly opened scope", () => {
  const h = appHarness();
  const listeners = new Set(), scope = {isConnected: true};
  let cancelled = 0;
  const dialog = {open: true, addEventListener(_type, listener) { listeners.add(listener); }, removeEventListener(_type, listener) { listeners.delete(listener); }};
  h.context.dialogFixture = dialog;
  h.context.scopeFixture = scope;
  h.context.cancelFixture = () => cancelled++;
  h.run("whenDialogClosed(dialogFixture, scopeFixture, cancelFixture)");
  for (const listener of listeners) listener(); // A close queued before this scope opened.
  assert.equal(cancelled, 0);
  assert.equal(listeners.size, 1, "Stale close consumed the scope's actual cleanup listener");
  dialog.open = false;
  for (const listener of listeners) listener();
  assert.equal(cancelled, 1);
  assert.equal(listeners.size, 0);
  dialog.open = true;
  scope.isConnected = false;
  h.run("whenDialogClosed(dialogFixture, scopeFixture, cancelFixture)");
  for (const listener of listeners) listener();
  assert.equal(cancelled, 2, "Detached old scope was not cleaned up");
});

function notificationHarness({delayHistory = false} = {}) {
  const history = deferred(), ack = deferred(), latest = deferred();
  const dot = {hidden: true}, refresh = {hidden: true}, error = {};
  const host = {isConnected: true, innerHTML: "", querySelector: selector => selector === "[data-notifications-refresh]" ? refresh : error};
  const bell = {querySelector: () => dot, setAttribute() {}};
  const listeners = {}, actions = [], queries = [], notices = [];
  const context = {
    window: {}, state: {profiles: [], experience: {}}, URLSearchParams,
    document: {
      addEventListener(type, listener) { (listeners[type] ||= []).push(listener); },
      querySelector(selector) { return selector === ".notification-panel" ? host : selector === ".notification-bell" ? bell : selector === "[data-notifications-refresh]" ? refresh : null; },
    },
    $: () => ({open: true}), modal() {}, active: () => null, ic: () => "", esc: value => String(value), notice: message => notices.push(message),
    api(url) {
      const action = new URL("http://test" + url).searchParams.get("action");
      queries.push(action);
      if (action === "experienceNotifications") return history.promise;
      assert.equal(action, "experienceState");
      return latest.promise;
    },
    act(action, data) { actions.push({action, ...data}); return ack.promise; },
  };
  vm.createContext(context);
  vm.runInContext(source("experience.js"), context, {filename: "ui/experience.js"});
  const snapshot = {items: [{id: 1, created: 1, message: "Shown event", level: "info", read: false}], unread: 1, lastId: 1};
  if (!delayHistory) history.resolve(snapshot);
  function open() {
    listeners.click[0]({target: {closest: () => ({dataset: {experience: "notifications"}})}, preventDefault() {}});
    return flush();
  }
  return {context, dot, refresh, error, actions, queries, notices, history, ack, latest, snapshot, open, observe: context.window.ExperienceUI.observeNotifications};
}

test("reading an unchanged notification snapshot clears the dot without an extra fetch", async () => {
  const h = notificationHarness();
  await h.open();
  assert.equal(h.dot.hidden, false);
  assert.deepEqual(h.actions, [{action: "experienceReadNotifications", throughId: 1}]);
  h.ack.resolve({unread: 0, lastId: 1});
  await flush();
  assert.equal(h.dot.hidden, true);
  assert.deepEqual(h.queries, ["experienceNotifications"]);
});

test("a delayed read ACK cannot hide a new event observed by polling", async () => {
  const h = notificationHarness();
  await h.open();
  h.observe(1); // The server has read event 1 and added a new event 2.
  h.ack.resolve({unread: 0, lastId: 1}); // Response captured before event 2 arrived.
  await flush();
  assert.equal(h.dot.hidden, false, "The stale ACK hid the new event before the refresh");
  assert.equal(h.context.state.notificationUnread, 1);
  assert.deepEqual(h.queries, ["experienceNotifications", "experienceState"]);
  h.latest.resolve({notificationUnread: 1});
  await flush();
  assert.equal(h.dot.hidden, false);
  assert.equal(h.refresh.hidden, false);
});

test("a delayed post-ACK refresh also cannot replace a newer polling observation", async () => {
  const h = notificationHarness();
  await h.open();
  h.observe(1);
  h.ack.resolve({unread: 0, lastId: 1});
  await flush();
  h.observe(2);
  h.latest.resolve({notificationUnread: 0});
  await flush();
  assert.equal(h.context.state.notificationUnread, 2);
  assert.equal(h.dot.hidden, false);
});

test("an old history response does not clear new unread state or acknowledge unseen events", async () => {
  const h = notificationHarness({delayHistory: true});
  await h.open();
  h.observe(2);
  h.history.resolve({items: [], unread: 0, lastId: 0});
  await flush();
  assert.equal(h.context.state.notificationUnread, 2);
  assert.equal(h.dot.hidden, false);
  assert.equal(h.refresh.hidden, false);
  assert.deepEqual(h.actions, []);
});
