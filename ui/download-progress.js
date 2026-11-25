/* Only active file transfers and content installation belong in this panel.
   Completed results and ordinary actions continue to use notification history. */
(() => {
  "use strict";
  const activeStates = new Set(["queued", "downloading", "verifying"]);
  const contentCategories = new Set(["content", "modpack", "community"]);
  const installActions = new Set(["installModrinth", "packInstall", "communityInstall", "careApplyUpdates", "shareApply"]);
  const preparationActions = new Set(["play", "create", "prepareSteam", "enableSteam"]);
  const terminalItems = new Set(["installed", "already_installed", "incompatible", "error", "cancelled", "needs_review"]);
  const remembered = new Map(), preparing = new Map();
  let current = null, model = null, cancelling = "";
  const requestedCancellations = new Set();
  const panel = document.querySelector("#downloadProgress");
  if (!panel) return;
  const find = selector => panel.querySelector(selector);
  const finite = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  const bytes = value => {
    const count = finite(value);
    if (count >= 1024 ** 3) return (count / 1024 ** 3).toFixed(1) + " ГБ";
    if (count >= 1024 ** 2) return (count / 1024 ** 2).toFixed(1) + " МБ";
    if (count >= 1024) return (count / 1024).toFixed(1) + " КБ";
    return Math.floor(count) + " Б";
  };

  function actionFor(job) { return job?.action || remembered.get(job?.id) || ""; }
  function isContentJob(job) {
    if (!job?.busy) return false;
    if (job.kind === "catalog-install" || installActions.has(actionFor(job))) return true;
    // The game downloader reports its real stage and byte/file counts in this
    // message. Do not turn the complete play/login/game session into progress.
    return preparationActions.has(actionFor(job)) && /^(Скачиваем |Загружаем |Повторяем загрузку |Повтор загрузки |Резервный источник |Устанавливаем Java |Восстанавливаем недостающий файл |Проверяем локальные файлы игры|Подготовка файлов (?:Minecraft и Java|NeoForge)|Подготовка mappings Minecraft|Готовим гардероб в игре:|Библиотеки(?:[ ·…]|$)|Ресурсы игры(?:[ ·…]|$)|Установка Java(?:[ ·…]|$)|Настраиваем (?:загрузчик|Fabric|Quilt))/.test(job.message || "");
  }
  function tasksFor(job) {
    const content = (job?.contentTransfers?.tasks || []).filter(task => contentCategories.has(task.category));
    const updates = (job?.updater?.transfers?.tasks || []).filter(task => task.category === "launcher-update");
    const seen = new Set();
    return [...content, ...updates].filter(task => {
      if (!task.id || !activeStates.has(task.state) || seen.has(task.id)) return false;
      seen.add(task.id);
      return true;
    });
  }
  function describe(job) {
    const tasks = tasksFor(job), contentJob = isContentJob(job);
    const task = tasks.find(row => row.category !== "launcher-update" && row.state === "downloading")
      || tasks.find(row => row.category !== "launcher-update")
      || tasks.find(row => row.state === "downloading") || tasks[0];
    const pending = preparing.values().next().value;
    const update = job?.updater?.pending;
    const installingUpdate = update && ["preparing", "launching"].includes(update.stage);
    if (!task && !contentJob && !pending && !installingUpdate) return null;
    const catalog = contentJob && job.kind === "catalog-install";
    const result = {
      title: catalog ? "Установка дополнений" : contentJob ? "Подготовка сборки" : pending?.title || "Загрузка файлов",
      message: contentJob ? job.message || "Установка файлов…" : pending?.message || "",
      metric: "", summary: "", value: null, maximum: 1,
      details: catalog ? "batch" : contentJob || !pending ? "downloads" : "",
      cancel: null,
    };
    if (task) {
      if (!contentJob && !pending) result.title = task.category === "launcher-update" ? "Обновление BlockHarbor" : "Загрузка файлов";
      result.message = (task.state === "queued" ? "В очереди: " : task.state === "verifying" ? "Проверяем: " : "Скачиваем: ") + (task.title || "Файл");
      const total = finite(task.total), received = finite(task.bytes);
      result.metric = total ? bytes(received) + " / " + bytes(total) : received ? bytes(received) : "";
      if (task.state === "downloading" && total) {
        result.maximum = total;
        result.value = Math.min(received, total);
        result.metric = Math.min(100, Math.floor(received / total * 100)) + "% · " + result.metric;
      }
      if (task.state === "downloading" && finite(task.rate)) result.metric += (result.metric ? " · " : "") + bytes(task.rate) + "/с";
      if (tasks.length > 1) result.summary = "Активных загрузок: " + tasks.length;
      result.cancel = {type: "transfer", id: task.id, label: "Отменить загрузку", requested: false};
    } else if (installingUpdate && !contentJob && !pending) {
      result.title = "Обновление BlockHarbor";
      result.message = update.stage === "preparing" ? "Готовим установку обновления…" : "Устанавливаем обновление…";
    }
    if (catalog) {
      const items = Array.isArray(job.items) ? job.items : [];
      const completed = items.filter(item => terminalItems.has(item.status)).length;
      if (items.length) {
        result.summary = "Обработано дополнений: " + completed + " из " + items.length;
        if (!task) { result.value = completed; result.maximum = items.length; }
      }
      if (job.cancellable || job.cancelRequested) result.cancel = {
        type: "catalog", id: job.id, label: "Отменить установку", requested: Boolean(job.cancelRequested),
      };
    }
    return result;
  }
  function cancelKey(target) { return target ? target.type + ":" + target.id : ""; }
  function hidePopover() {
    if (typeof panel.hidePopover === "function" && panel.matches(":popover-open")) panel.hidePopover();
  }
  function relocate() {
    // Belong to the modal when it is open, so the panel is not made inert by
    // showModal(). The manual popover keeps it above the modal's scrolling area.
    const dialogs = [...document.querySelectorAll("dialog[open]")];
    const parent = dialogs[dialogs.length - 1] || document.body;
    if (panel.parentElement !== parent) {
      hidePopover();
      parent.append(panel);
    }
    if (panel.hidden) { hidePopover(); return; }
    if (typeof panel.showPopover === "function" && panel.hasAttribute("popover") && !panel.matches(":popover-open")) {
      try { panel.showPopover(); } catch { panel.removeAttribute("popover"); }
    }
  }
  function paint() {
    model = describe(current);
    panel.hidden = !model;
    if (!model) { relocate(); return; }
    find("[data-download-title]").textContent = model.title;
    find("[data-download-message]").textContent = model.message;
    const metric = find("[data-download-metric]"), summary = find("[data-download-summary]");
    metric.textContent = model.metric;
    metric.hidden = !model.metric;
    summary.textContent = model.summary;
    summary.hidden = !model.summary;
    const progress = find("progress");
    progress.max = model.maximum;
    if (model.value === null) progress.removeAttribute("value");
    else progress.value = model.value;
    progress.setAttribute("aria-label", model.value === null ? model.message : model.metric || model.summary);
    const details = find("[data-download-details]"), cancel = find("[data-download-cancel]");
    const batchVisible = Boolean(document.querySelector("#dialog[open] .batch-panel"));
    details.hidden = !model.details || model.details === "batch" && batchVisible;
    details.textContent = model.details === "batch" ? "Подробнее" : "Открыть загрузки";
    cancel.hidden = !model.cancel || model.cancel.type === "catalog" && batchVisible;
    const key = cancelKey(model.cancel);
    cancel.disabled = Boolean(key && (cancelling === key || requestedCancellations.has(key) || model.cancel.requested));
    cancel.textContent = cancel.disabled ? "Отменяем…" : model.cancel?.label || "Отменить";
    find(".download-progress-actions").hidden = details.hidden && cancel.hidden;
    relocate();
  }
  function observe(job) {
    current = job;
    const targets = new Set(tasksFor(job).map(task => "transfer:" + task.id));
    if (job?.busy && job.kind === "catalog-install") targets.add("catalog:" + job.id);
    for (const key of requestedCancellations) if (!targets.has(key)) requestedCancellations.delete(key);
    if (job?.id && !job.busy) remembered.delete(job.id);
    paint();
  }
  panel.addEventListener("click", async event => {
    const target = event.target.closest("button");
    if (!target || target.disabled || !model) return;
    if (target.hasAttribute("data-download-details")) {
      if (model.details === "batch") window.BatchUI?.open();
      else if (model.details) {
        if (document.querySelector("#dialog")?.open) closeDialog();
        await navigateFresh("settings", "downloads").catch(error => notice(error.message, true));
      }
      return;
    }
    if (!target.hasAttribute("data-download-cancel") || !model.cancel) return;
    const operation = {...model.cancel}, key = cancelKey(operation);
    if (cancelling || requestedCancellations.has(key)) return;
    cancelling = key;
    paint();
    try {
      await act(operation.type === "catalog" ? "cancelCatalogInstall" : "transferCancel",
        operation.type === "catalog" ? {job: operation.id} : {id: operation.id});
      requestedCancellations.add(key);
      await tick();
    } catch (error) { notice(error.message, true); }
    finally { cancelling = ""; paint(); }
  });
  document.addEventListener("close", relocate, true);
  if (typeof MutationObserver !== "undefined") {
    new MutationObserver(relocate).observe(document.body, {subtree: true, attributes: true, attributeFilter: ["open"]});
  }
  window.DownloadProgressUI = {
    observe, relocate,
    remember(id, action) {
      if (!id) return;
      remembered.set(id, action);
      if (remembered.size > 8) remembered.delete(remembered.keys().next().value);
    },
    begin(id, title, message) { preparing.set(id, {title, message}); paint(); schedulePoll(); },
    end(id) { preparing.delete(id); paint(); schedulePoll(); },
    isActive: () => Boolean(model),
  };
})();
