// Exercise the production progress controller with real snapshot shapes.
// Browser geometry remains part of the existing release UI smoke tests.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");

function element(attributes = {}) {
  return {
    attributes: {...attributes}, listeners: {}, hidden: false, disabled: false, textContent: "", parentElement: null,
    setAttribute(name, value) { this.attributes[name] = String(value); },
    hasAttribute(name) { return Object.hasOwn(this.attributes, name); },
    removeAttribute(name) { delete this.attributes[name]; if (name === "value") delete this.value; },
    addEventListener(type, callback) { this.listeners[type] = callback; },
    closest(selector) { return selector === "button" ? this : null; },
    append(child) { child.parentElement = this; },
  };
}
function harness() {
  const fields = Object.fromEntries([
    "[data-download-title]", "[data-download-message]", "[data-download-metric]", "[data-download-summary]", "progress", ".download-progress-actions",
  ].map(selector => [selector, element()]));
  fields["[data-download-details]"] = element({"data-download-details": ""});
  fields["[data-download-cancel]"] = element({"data-download-cancel": ""});
  const panel = element({popover: "manual"}), body = element(), dialog = element();
  panel.hidden = true;
  panel.parentElement = body;
  panel.querySelector = selector => fields[selector];
  panel.popoverOpen = false;
  panel.matches = selector => selector === ":popover-open" && panel.popoverOpen;
  panel.showPopover = () => { panel.popoverOpen = true; };
  panel.hidePopover = () => { panel.popoverOpen = false; };
  dialog.open = false;
  let batchVisible = false, onAct = async () => ({ok: true}), scheduled = 0, batchOpens = 0;
  const calls = [], notices = [], navigations = [];
  const context = {
    window: {BatchUI: {open() { batchOpens++; }}},
    document: {
      body,
      querySelector(selector) {
        if (selector === "#downloadProgress") return panel;
        if (selector === "#dialog") return dialog;
        if (selector === "#dialog[open] .batch-panel") return dialog.open && batchVisible ? {} : null;
        return null;
      },
      querySelectorAll: () => dialog.open ? [dialog] : [],
      addEventListener() {},
    },
    act: async (action, params) => { calls.push({action, params: JSON.parse(JSON.stringify(params))}); return onAct(action, params); },
    notice: (message, bad) => notices.push({message, bad}),
    tick: async () => {}, schedulePoll: () => { scheduled++; },
    closeDialog: () => { dialog.open = false; },
    navigateFresh: async (...args) => navigations.push(args),
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "ui", "download-progress.js"), "utf8"), context);
  return {ui: context.window.DownloadProgressUI, panel, fields, body, dialog, calls, notices, navigations,
    click: selector => panel.listeners.click({target: fields[selector]}),
    set act(callback) {onAct = callback;}, set batchVisible(value) {batchVisible = value;},
    get scheduled() {return scheduled;}, get batchOpens() {return batchOpens;},
  };
}
const transfer = overrides => ({id: "file-a", category: "content", state: "downloading", title: "mod.jar", bytes: 1024, total: 4096, rate: 512, ...overrides});
const snapshot = overrides => ({id: "job-a", busy: false, message: "Готово", contentTransfers: {tasks: []}, ...overrides});

test("ordinary actions and their results never open the download panel", () => {
  const h = harness();
  for (const action of ["saveProfile", "signin", "experiencePreferences", "updateCheck", "purgeBuild", "communityPublish"]) {
    h.ui.remember("job-a", action);
    h.ui.observe(snapshot({action, busy: true, message: "Скачиваем сведения для обычного действия…"}));
    assert.equal(h.panel.hidden, true, action);
    h.ui.observe(snapshot({action, ok: false, message: "Действие не удалось"}));
    assert.equal(h.panel.hidden, true, action);
  }
  assert.deepEqual(h.notices, [], "Progress must not duplicate completion notifications");
});

test("file percentages, bytes and speed come from active transfer counters", () => {
  const h = harness();
  h.ui.observe(snapshot({contentTransfers: {tasks: [transfer()]}}));
  assert.equal(h.panel.hidden, false);
  assert.equal(h.fields.progress.value, 1024);
  assert.equal(h.fields.progress.max, 4096);
  assert.match(h.fields["[data-download-metric]"].textContent, /^25% · 1.0 КБ \/ 4.0 КБ · 512 Б\/с$/);
  h.ui.observe(snapshot({contentTransfers: {tasks: [transfer({state: "verifying", bytes: 4096})]}}));
  assert.equal(h.fields.progress.value, undefined, "Verification has no invented percentage");
  assert.match(h.fields["[data-download-message]"].textContent, /^Проверяем:/);
  h.ui.observe(snapshot({contentTransfers: {tasks: [transfer({state: "completed"})]}}));
  assert.equal(h.panel.hidden, true);
  assert.equal(h.ui.isActive(), false);
});

test("unknown totals remain indeterminate and failed transfers disappear", () => {
  const h = harness();
  h.ui.observe(snapshot({contentTransfers: {tasks: [transfer({total: null, bytes: 9000})]}}));
  assert.equal(h.fields.progress.value, undefined);
  assert.doesNotMatch(h.fields["[data-download-metric]"].textContent, /%/);
  assert.match(h.fields["[data-download-metric]"].textContent, /8.8 КБ/);
  for (const state of ["failed", "cancelled", "interrupted"]) {
    h.ui.observe(snapshot({contentTransfers: {tasks: [transfer({state})]}}));
    assert.equal(h.panel.hidden, true, state);
  }
});

test("pack installation remains visible between files and hides after failure", () => {
  const h = harness();
  h.ui.observe(snapshot({action: "packInstall", busy: true, message: "Применяем настройки модпака…"}));
  assert.equal(h.panel.hidden, false);
  assert.equal(h.fields.progress.value, undefined);
  assert.equal(h.fields["[data-download-cancel]"].hidden, true, "No unsupported job cancellation should be offered");
  h.ui.observe(snapshot({action: "packInstall", busy: false, ok: false, message: "Файл повреждён"}));
  assert.equal(h.panel.hidden, true);
  assert.deepEqual(h.notices, []);
});

test("catalog installation shows processed item counts and cancels its exact job", async () => {
  const h = harness();
  const job = snapshot({kind: "catalog-install", busy: true, cancellable: true, items: [
    {status: "installed"}, {status: "downloading"}, {status: "queued"},
  ]});
  h.ui.observe(job);
  assert.equal(h.fields.progress.value, 1);
  assert.equal(h.fields.progress.max, 3);
  assert.match(h.fields["[data-download-summary]"].textContent, /1 из 3/);
  await h.click("[data-download-cancel]");
  await h.click("[data-download-cancel]");
  assert.deepEqual(h.calls, [{action: "cancelCatalogInstall", params: {job: "job-a"}}]);
  assert.equal(h.fields["[data-download-cancel]"].disabled, true);
  h.ui.observe({...job, id: "job-b"});
  assert.equal(h.fields["[data-download-cancel]"].disabled, false, "Old cancellation cannot disable a newer install");
});

test("transfer cancellation errors stay in notification history and allow retry", async () => {
  const h = harness();
  h.ui.observe(snapshot({contentTransfers: {tasks: [transfer()]}}));
  h.act = async () => { throw new Error("Не удалось отменить загрузку"); };
  await h.click("[data-download-cancel]");
  assert.deepEqual(h.calls, [{action: "transferCancel", params: {id: "file-a"}}]);
  assert.deepEqual(h.notices, [{message: "Не удалось отменить загрузку", bad: true}]);
  assert.equal(h.panel.hidden, false);
  assert.equal(h.fields["[data-download-cancel]"].disabled, false);
});

test("preparing a selected pack is visible before job acceptance without losing its selector", () => {
  const h = harness();
  h.ui.begin("preview:a", "Загрузка сборки", "Загружаем и проверяем модпак…");
  assert.equal(h.panel.hidden, false);
  assert.equal(h.fields.progress.value, undefined);
  assert.equal(h.fields["[data-download-details]"].hidden, true, "Navigation would abandon the pending selector");
  h.ui.observe(snapshot({contentTransfers: {tasks: [transfer({category: "modpack"})]}}));
  assert.equal(h.fields.progress.value, 1024);
  assert.equal(h.fields["[data-download-details]"].hidden, true);
  h.ui.observe(snapshot({action: "packInstall", busy: true, message: "Устанавливаем сборку"}));
  h.ui.end("preview:a");
  assert.equal(h.panel.hidden, false, "Accepted installation must survive preview cleanup");
  h.ui.observe(snapshot());
  assert.equal(h.panel.hidden, true);
  assert.equal(h.scheduled, 2);
});

test("Minecraft download stages show real text without claiming the game session is a download", () => {
  const h = harness();
  h.ui.remember("job-a", "play");
  h.ui.observe(snapshot({busy: true, message: "Скачиваем minecraft.jar · 10.0 МБ / 30.0 МБ"}));
  assert.equal(h.panel.hidden, false);
  assert.equal(h.fields.progress.value, undefined, "An unstructured message must not produce a guessed percentage");
  assert.match(h.fields["[data-download-message]"].textContent, /10.0 МБ \/ 30.0 МБ/);
  for (const message of [
    "Проверяем локальные файлы игры…",
    "Подготовка файлов NeoForge · проверяем контрольную сумму…",
    "Подготовка файлов NeoForge · проверяем сохранённый установщик…",
    "Повтор загрузки neoforge-installer.jar · 2 / 6",
    "Резервный источник neoforged.forgecdn.net · neoforge-installer.jar",
  ]) {
    h.ui.observe(snapshot({action: "play", busy: true, message}));
    assert.equal(h.panel.hidden, false, message);
    assert.equal(h.fields["[data-download-message]"].textContent, message);
    assert.equal(h.fields.progress.value, undefined, "Recovery must not invent a percentage");
    h.ui.observe(snapshot({action: "saveProfile", busy: true, message}));
    assert.equal(h.panel.hidden, true, "Only game preparation belongs in the download panel");
  }
  h.ui.observe(snapshot({action: "play", busy: true, message: "Готовим гардероб в игре: QuickSkin…"}));
  assert.equal(h.panel.hidden, false);
  assert.equal(h.fields.progress.value, undefined);
  h.ui.observe(snapshot({action: "saveProfile", busy: true, message: "Готовим гардероб в игре: QuickSkin…"}));
  assert.equal(h.panel.hidden, true, "The wardrobe stage must not turn ordinary profile saving into a download");
  for (const message of ["Запускается Minecraft…", "Войди в Microsoft в открывшемся браузере.", "Проверка профиля Minecraft Java Edition…"]) {
    h.ui.observe(snapshot({busy: true, message}));
    assert.equal(h.panel.hidden, true, message);
  }
  h.ui.observe(snapshot({action: "play", gameRunning: true}));
  assert.equal(h.panel.hidden, true);
});

test("launcher downloads are visible while ordinary update checks remain silent", () => {
  const h = harness();
  h.ui.observe(snapshot({updater: {autoChecking: true, transfers: {tasks: []}}}));
  assert.equal(h.panel.hidden, true);
  h.ui.observe(snapshot({updater: {transfers: {tasks: [transfer({category: "launcher-update"})]}}}));
  assert.equal(h.panel.hidden, false);
  assert.equal(h.fields["[data-download-title]"].textContent, "Обновление BlockHarbor");
  h.ui.observe(snapshot({updater: {ready: true, transfers: {tasks: [transfer({category: "launcher-update", state: "completed"})]}}}));
  assert.equal(h.panel.hidden, true);
});

test("a modal keeps progress interactive and its own detailed batch actions are not duplicated", async () => {
  const h = harness();
  h.ui.observe(snapshot({kind: "catalog-install", busy: true, cancellable: true, items: [{status: "queued"}]}));
  assert.equal(h.panel.parentElement, h.body);
  assert.equal(h.panel.popoverOpen, true);
  await h.click("[data-download-details]");
  assert.equal(h.batchOpens, 1);
  h.dialog.open = true;
  h.batchVisible = true;
  h.ui.observe(snapshot({kind: "catalog-install", busy: true, cancellable: true, items: [{status: "downloading"}]}));
  assert.equal(h.panel.parentElement, h.dialog);
  assert.equal(h.panel.popoverOpen, true);
  assert.equal(h.fields["[data-download-details]"].hidden, true);
  assert.equal(h.fields["[data-download-cancel]"].hidden, true);
  h.dialog.open = false;
  h.ui.relocate();
  assert.equal(h.panel.parentElement, h.body);
  h.ui.observe(snapshot());
  assert.equal(h.panel.hidden, true);
  assert.equal(h.panel.popoverOpen, false);
});
