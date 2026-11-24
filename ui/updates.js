/* Release and transfer UI. Loaded after app.js, using the shared API and theme. */
(() => {
  "use strict";
  let data = null;
  let timer = 0;
  let fetching = false;
  let lastTransferMarkup = "";
  let lastReleaseMarkup = "";
  let transferHistoryExpanded = false;
  let savingAutomatic = false;
  let savingChannel = false;
  let pendingChannel = null;
  let channelRevision = 0;
  let progressData = null;
  let completedCheck = null;
  const activeTransfers = new Set(["queued", "downloading", "verifying"]);
  const recentTransferLimit = 4;
  const labels = {
    queued: "В очереди", downloading: "Загрузка", verifying: "Проверка",
    completed: "Готово", cancelled: "Отменено", failed: "Ошибка", interrupted: "Прервано",
  };
  const button = (action, text, icon, extra = "", disabled = false) =>
    `<button class="btn ${extra}" type="button" data-updates-action="${esc(action)}" ${disabled ? "disabled" : ""}>${ic(icon)}${esc(text)}</button>`;
  const formatDate = (seconds) => seconds ? new Date(seconds * 1000).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }) : "Ещё не проверяли";

  function updateNotice(snapshot) {
    const pending = snapshot.pending;
    const version = pending?.version || snapshot.release?.version || "";
    const label = "BlockHarbor" + (version ? " " + version : "");
    if (pending && ["preparing", "launching"].includes(pending.stage)) {
      return {
        id: `install:${version}:${pending.stage}`, busy: true,
        message: pending.stage === "preparing"
          ? `Готовим обновление ${label}: проверяем установщик и сохраняем предыдущую версию…`
          : pending.automatic
            ? `Устанавливаем ${label}. Лаунчер ненадолго закроется и откроется после обновления.`
            : `Открываем установщик ${label}. Лаунчер закроется для установки.`,
      };
    }
    const tasks = snapshot.transfers?.tasks || [];
    const matchesVersion = (task) => !version || task.title?.endsWith(version);
    const transfer = tasks.find((task) => activeTransfers.has(task.state) && matchesVersion(task))
      || tasks.find((task) => activeTransfers.has(task.state));
    if (transfer) {
      const total = Math.max(0, Number(transfer.total) || 0);
      const bytes = Math.max(0, Number(transfer.bytes) || 0);
      const amount = total ? ` — ${Math.min(100, Math.floor(bytes / total * 100))}% · ${fmt(bytes)} / ${fmt(total)}` : bytes ? ` — ${fmt(bytes)}` : "";
      const title = transfer.title || label;
      return {
        id: `download:${transfer.id}:${transfer.attempt || 0}:${transfer.state}`, busy: true,
        message: transfer.state === "verifying" ? `Проверяем загруженный файл ${title}…`
          : transfer.state === "queued" ? `${title}: загрузка ожидает своей очереди…`
            : `Скачиваем ${title}${amount}`,
      };
    }
    if (snapshot.autoChecking) return {id: "checking", busy: true, message: "Проверяем обновления BlockHarbor…"};
    if (snapshot.error) return {
      id: `error:${snapshot.checkStatus?.retryAt || snapshot.checkedAt || 0}:${snapshot.error}`,
      message: snapshot.error, bad: true,
    };
    if (pending) return {
      id: `queued:${version}:${Boolean(pending.automatic)}`,
      message: pending.automatic
        ? `${label} готов к установке. Обновление начнётся после завершения игр, операций и закрытия остальных окон лаунчера.`
        : `Установка ${label} запланирована. После завершения игр и операций откроется установщик.`,
    };
    if (snapshot.ready && snapshot.available) return {
      id: `ready:${version}`, message: `${label} скачан и проверен. Обновление готово к установке.`,
    };
    const recent = tasks.find(matchesVersion);
    if (recent && ["failed", "interrupted", "cancelled"].includes(recent.state)) return {
      id: `download:${recent.id}:${recent.attempt || 0}:${recent.state}`,
      message: recent.message || (recent.state === "cancelled" ? "Загрузка обновления отменена." : "Не удалось скачать обновление BlockHarbor. Повтори загрузку в настройках."),
      bad: recent.state !== "cancelled",
    };
    if (snapshot.available) return {
      id: `available:${snapshot.channel}:${version}`,
      message: snapshot.release?.compatible === false
        ? `${label} использует другой формат данных. Автоматическая установка недоступна.`
        : `Доступен ${label}. Подробности — в «Настройки → Загрузки и обновления».`,
    };
    return completedCheck;
  }

  function observe(snapshot) {
    if (!snapshot) return;
    if (snapshot.autoChecking) completedCheck = null;
    else if (progressData?.autoChecking && snapshot.checkedAt > (progressData.checkedAt || 0)
      && !snapshot.error && !snapshot.available) {
      completedCheck = {id: `checked:${snapshot.checkedAt}`, message: "У тебя актуальная версия BlockHarbor."};
    }
    progressData = snapshot;
    const result = snapshot.startupResult;
    backgroundNotice("update-result", result ? {...result, bad: !result.ok, persist: true} : null);
    backgroundNotice("updater", updateNotice(snapshot));
  }

  function isActive() {
    return Boolean(progressData?.autoChecking
      || ["preparing", "launching"].includes(progressData?.pending?.stage)
      || progressData?.transfers?.tasks.some((task) => activeTransfers.has(task.state)));
  }

  function render({embedded = false} = {}) {
    const actions = button("updateCheck", "Проверить обновления", "refresh");
    const heading = embedded
      ? `<div class="settings-updates-head"><div><h2>Загрузки и обновления</h2><p class="muted">Обновление BlockHarbor и очередь скачиваемых файлов.</p></div>${actions}</div>`
      : head("Загрузки и обновления", actions, "Версии лаунчера, очередь файлов и результаты проверки.");
    return heading +
      `<div id="updatesScreen"><div class="settings-grid"><section class="panel updater-automatic"><label class="updater-auto-control" for="automaticUpdates"><span><strong>Автоматически обновлять BlockHarbor</strong><span>Проверка при запуске и каждые 10 минут, пока открыт лаунчер.</span></span><input type="checkbox" id="automaticUpdates" role="switch" aria-describedby="automaticUpdateHelp" ${data?.autoEnabled ? "checked" : ""} ${data ? "" : "disabled"}></label><p id="automaticUpdateHelp" class="muted">Лаунчер скачает и проверит новую версию из выбранного канала. Когда все игры, загрузки и операции завершатся и останется одно окно, BlockHarbor обновится и откроется снова. После обычного закрытия проверки и загрузки прекращаются.</p><p id="automaticUpdateStatus" class="updater-auto-status" role="status"></p></section><section class="panel updater-channel"><div><h2>Канал обновлений</h2><p class="muted">Стабильный — для обычной игры. Тестовый — для ранних версий BlockHarbor.</p></div><label>Канал<select id="updateChannel" class="field" aria-label="Канал обновлений" ${savingChannel || !data ? "disabled" : ""} ${savingChannel ? 'aria-busy="true"' : ""}><option value="stable">Стабильный</option><option value="test" ${(pendingChannel || data?.channel) === "test" ? "selected" : ""}>Тестовый</option></select></label></section></div><div id="updateRelease" aria-live="polite">${data ? releaseMarkup() : '<div class="empty">Получаем состояние обновлений…</div>'}</div><section class="panel transfer-panel"><div class="transfer-heading"><div><h2>Загрузки</h2><p class="muted">Файл появляется в сборке после проверки контрольной суммы.</p></div>${button("transferClear", "Очистить историю", "trash")}</div><div id="transferList">${data ? transfersMarkup() : '<p class="muted">Загружаем очередь…</p>'}</div></section></div>`;
  }

  function releaseMarkup() {
    if (!data) return "";
    const release = data.release;
    const busy = data.transfers?.tasks.some((task) => task.category === "launcher-update" && ["queued", "downloading", "verifying"].includes(task.state));
    let actions = "";
    let heading = `Установлен BlockHarbor ${esc(data.current)}`;
    let body = `<p class="muted">Последняя проверка: ${esc(formatDate(data.checkedAt))}.</p>`;
    if (data.checkStatus?.message) body += `<p class="muted">${esc(data.checkStatus.message)}</p>`;
    if (data.checkStatus?.retryAt > Date.now() / 1000) body += `<p class="muted">Повторная сетевая проверка доступна с: ${esc(formatDate(data.checkStatus.retryAt))}.</p>`;
    if (data.available && release) {
      heading = `Доступен BlockHarbor ${esc(release.version)}`;
      body += `<div class="tags"><span class="pill">${release.prerelease ? "Тестовая версия" : "Стабильная версия"}</span><span class="pill">${fmt(release.size)}</span><span class="pill">${release.compatible ? "Формат данных совместим" : "Нужна миграция данных"}</span></div>`;
      if (!release.compatible) body += '<p class="update-problem">Эта версия использует другой формат данных. Автоматическая установка недоступна.</p>';
      else if (data.ready) {
        body += '<p class="update-ready">Установщик скачан и проверен. Предыдущий исполняемый файл и копия настроек сохраняются перед установкой.</p>';
        if (!data.pending) actions += button("installDialog", "Установить обновление", "download", "main", !data.windows);
      } else {
        actions += button("updateDownload", busy ? "Загрузка выполняется" : "Скачать обновление", "download", "main", busy);
      }
      body += `<details class="update-notes"><summary>Что изменилось</summary><div>${esc(release.notes)}</div><a href="${esc(release.url)}" target="_blank" rel="noopener noreferrer">Релиз на GitHub ${ic("link")}</a></details>`;
    } else if (release) {
      body += '<p class="update-ready">Более новая версия в выбранном канале не найдена.</p>';
    }
    if (data.pending) {
      const stage = data.pending.stage;
      const automatic = data.pending.automatic;
      body += `<div class="update-pending" role="status">${ic("refresh")}<div><strong>${stage === "queued" ? automatic ? "Автоматическое обновление готово" : "Установка запланирована" : stage === "preparing" ? "Сохраняем предыдущую версию" : automatic ? "Устанавливаем обновление" : "Открываем установщик"}</strong><p>${stage === "queued" ? automatic ? "Закрой другие окна BlockHarbor. После завершения игр, загрузок и операций приложение обновится и откроется снова." : "Закрой другие окна BlockHarbor. После выхода из всех игр и завершения операций откроется установщик." : automatic ? "BlockHarbor ненадолго закроется и автоматически откроется после успешной установки." : "BlockHarbor закроется после подготовки помощника установки."}</p></div></div>`;
      if (stage !== "launching") actions += button("updateCancelInstall", automatic ? "Отложить до следующего запуска" : "Отменить установку", "close");
    }
    if (data.rollback && !data.pending) {
      actions += button("rollbackDialog", `Вернуться к ${data.rollback.version}`, "back", "", !data.windows);
    }
    if (!data.windows) body += '<p class="muted">Автоматическая установка .exe выполняется в Windows. Сейчас открыт запуск для разработки.</p>';
    if (data.error) body += `<p class="update-problem" role="alert">${esc(data.error)}</p>`;
    return `<section class="panel updater-release"><div class="update-release-icon">${ic("cube")}</div><div class="update-release-info"><h2>${heading}</h2>${body}<div class="actions">${actions}</div><p class="update-trust">${esc(data.trust)}</p></div></section>`;
  }

  function transfersMarkup() {
    const tasks = data?.transfers?.tasks || [];
    const job = state?.job;
    const liveJob = job?.busy ? `<div class="transfer-stage"><span class="transfer-spinner">${ic("refresh")}</span><div><strong>Текущая операция</strong><p>${esc(job.message)}</p></div></div>` : "";
    if (!tasks.length) return liveJob + '<div class="empty">Здесь появятся скачиваемые файлы и результаты их проверки.</div>';
    const row = (task) => {
      const running = ["queued", "downloading", "verifying"].includes(task.state);
      const total = Number(task.total);
      const bytes = Number(task.bytes) || 0;
      const progress = running ? `<progress ${total > 0 ? `max="${total}" value="${Math.min(bytes, total)}"` : ""} aria-label="Загрузка ${esc(task.title)}"></progress>` : "";
      const amount = total > 0 ? `${fmt(bytes)} / ${fmt(total)}` : bytes ? fmt(bytes) : "";
      const speed = task.state === "downloading" && task.rate > 0 ? `${fmt(task.rate)}/с` : "";
      const actions = running ? `<button class="icon-button" data-transfer-cancel="${esc(task.id)}" aria-label="Отменить загрузку ${esc(task.title)}">${ic("close")}</button>` : task.retryable && task.state !== "completed" ? `<button class="btn" data-transfer-retry="${esc(task.id)}">${ic("refresh")}Повторить</button>` : "";
      return `<article class="transfer-row transfer-${esc(task.state)}"><div class="transfer-icon">${ic(task.state === "completed" ? "check" : task.state === "failed" ? "close" : "download")}</div><div class="transfer-info"><div class="transfer-title"><strong>${esc(task.title)}</strong><span class="pill">${esc(labels[task.state] || task.state)}</span></div>${progress}<div class="transfer-metrics"><span>${esc(amount)}${speed ? " · " + esc(speed) : ""}${task.resumed ? " · продолжена" : ""}</span><span>${esc(task.host || "")}</span></div><p class="transfer-message">${esc(task.message || "")}${!running && !task.retryable && ["failed", "cancelled", "interrupted"].includes(task.state) ? " Повтори установку из исходного окна." : ""}</p></div><div class="transfer-actions">${actions}</div></article>`;
    };
    const recent = tasks.slice(0, recentTransferLimit).map(row).join("");
    const older = tasks.slice(recentTransferLimit);
    if (!older.length) return liveJob + recent;
    return liveJob + recent +
      `<div class="transfer-history-controls"><button class="btn transfer-history-toggle" type="button" data-transfer-history aria-expanded="${transferHistoryExpanded}" aria-controls="transferHistory">${ic("down")}${transferHistoryExpanded ? "Скрыть историю" : "Показать историю"} (${older.length})</button></div>` +
      `<div id="transferHistory" ${transferHistoryExpanded ? "" : "hidden"}>${transferHistoryExpanded ? older.map(row).join("") : ""}</div>`;
  }

  function paint() {
    const screen = document.querySelector("#updatesScreen");
    if (!screen || !data) return;
    const channel = document.querySelector("#updateChannel");
    if (channel) {
      channel.disabled = savingChannel;
      if (savingChannel) channel.value = pendingChannel;
      else if (document.activeElement !== channel) channel.value = data.channel;
    }
    const automatic = document.querySelector("#automaticUpdates");
    if (automatic && !savingAutomatic) {
      automatic.checked = Boolean(data.autoEnabled);
      automatic.disabled = false;
    }
    const automaticStatus = document.querySelector("#automaticUpdateStatus");
    if (automaticStatus) automaticStatus.textContent = !data.autoEnabled
      ? "Автоматическое обновление выключено. Ручные действия доступны ниже."
      : data.autoFailure && data.autoFailure.version === data.release?.version
        ? `Автоустановка версии ${data.autoFailure.version} приостановлена после ошибки. Повторить можно кнопкой «Установить обновление».`
        : data.autoSnoozed && data.autoSnoozed === data.release?.version
          ? `Обновление до ${data.autoSnoozed} отложено до следующего запуска.`
          : data.autoChecking ? "Проверяем обновление в выбранном канале…" : "Автоматическое обновление включено.";
    const release = releaseMarkup();
    if (release !== lastReleaseMarkup) {
      const open = document.querySelector(".update-notes")?.open;
      document.querySelector("#updateRelease").innerHTML = release;
      if (open && document.querySelector(".update-notes")) document.querySelector(".update-notes").open = true;
      lastReleaseMarkup = release;
    }
    const transfers = transfersMarkup();
    if (transfers !== lastTransferMarkup) {
      const focus = document.activeElement?.closest("[data-transfer-cancel], [data-transfer-retry], [data-transfer-history]");
      const focusedId = focus?.dataset.transferCancel || focus?.dataset.transferRetry;
      const focusKind = focus?.dataset.transferCancel ? "cancel" : "retry";
      const historyFocused = focus?.hasAttribute("data-transfer-history");
      document.querySelector("#transferList").innerHTML = transfers;
      const restoreFocus = focusedId
        ? document.querySelector(`[data-transfer-${focusKind}="${CSS.escape(focusedId)}"]`) || document.querySelector("[data-transfer-history]")
        : historyFocused ? document.querySelector("[data-transfer-history]") : null;
      restoreFocus?.focus({ preventScroll: true });
      lastTransferMarkup = transfers;
    }
  }

  async function load(silent = false) {
    if (fetching || !document.querySelector("#updatesScreen")) return;
    fetching = true;
    const revision = channelRevision;
    try {
      const result = await api("/api/feature?action=updateState");
      if (revision === channelRevision) {
        data = result;
        paint();
      }
    } catch (error) {
      if (!silent) notice(error.message, true);
    } finally {
      fetching = false;
      clearTimeout(timer);
      if (document.querySelector("#updatesScreen")) timer = setTimeout(() => load(true), 1500);
    }
  }

  async function saveChannel(control) {
    if (savingChannel || !data) return;
    const previous = data.channel;
    const selected = control.value;
    const focused = document.activeElement === control;
    savingChannel = true;
    pendingChannel = selected;
    channelRevision++;
    control.disabled = true;
    control.setAttribute("aria-busy", "true");
    try {
      await act("updateChannel", {channel: selected});
      data.channel = selected;
      notice("Канал обновлений сохранён.");
    } catch (error) {
      data.channel = previous;
      notice(error.message, true);
    } finally {
      channelRevision++;
      savingChannel = false;
      pendingChannel = null;
      const current = document.querySelector("#updateChannel");
      if (current) {
        current.value = data.channel;
        current.disabled = false;
        current.removeAttribute("aria-busy");
        if (current === control && focused && [control, document.body].includes(document.activeElement)) {
          current.focus({preventScroll: true});
        }
      }
      await load(true);
      await tick();
    }
  }

  function mount() {
    if (!document.querySelector("#updatesScreen")) {
      clearTimeout(timer);
      return;
    }
    lastReleaseMarkup = lastTransferMarkup = "";
    paint();
    load();
  }

  function installDialog(rollback = false) {
    const version = rollback ? data?.rollback?.version : data?.release?.version;
    if (!version) return;
    const running = Boolean(state.gameRunning || state.job?.busy || state.launcherCount > 1);
    modal(rollback ? "Вернуться к предыдущей версии" : "Установить обновление", `<p>BlockHarbor ${esc(version)} ${rollback ? "заменит текущую версию приложения" : "будет установлен после закрытия лаунчера"}. Сборки и миры останутся в текущем состоянии.</p><p>${rollback ? "Совместимость формата данных проверена. Откат приложения не откатывает изменения внутри миров." : "Перед установкой сохранятся предыдущий исполняемый файл и копия настроек. Откроется обычный мастер установки Windows."}</p>${state.launcherCount > 1 ? '<p>Для установки закрой остальные окна BlockHarbor. Запущенные игры можно завершить обычным способом.</p>' : ""}<div class="actions">${button(rollback ? "rollbackAfterGame" : "installAfterGame", running ? "Установить после завершения сеансов" : "Закрыть лаунчер и установить", "download", "main")}${button("closeDialog", "Отмена", "close")}</div>`);
  }

  async function dispatch(action) {
    if (![
      "updateCheck", "updateDownload", "updateCancelInstall", "transferClear",
      "installDialog", "rollbackDialog", "installAfterGame", "rollbackAfterGame", "closeDialog",
    ].includes(action)) return false;
    try {
      if (action === "closeDialog") closeDialog();
      else if (action === "installDialog" || action === "rollbackDialog") installDialog(action === "rollbackDialog");
      else if (action === "updateCheck") await runJob("updateCheck");
      else if (action === "installAfterGame" || action === "rollbackAfterGame") {
        closeDialog();
        await act(action === "rollbackAfterGame" ? "updateRollback" : "updateInstall", { when: "idle", acknowledge: true });
        notice("Установка запланирована. Закрой остальные окна BlockHarbor и заверши игры.");
      } else {
        const postponed = action === "updateCancelInstall" && data?.pending?.automatic;
        await act(action);
        if (action === "transferClear") transferHistoryExpanded = false;
        if (action === "updateDownload") notice("Обновление добавлено в очередь загрузок.");
        if (action === "updateCancelInstall") notice(postponed ? "Автоматическое обновление отложено до следующего запуска." : "Отложенная установка отменена.");
      }
      await load(true);
      await tick();
    } catch (error) {
      notice(error.message, true);
    }
    return true;
  }

  document.addEventListener("click", async (event) => {
    const history = event.target.closest("[data-transfer-history]");
    if (history) {
      event.preventDefault();
      transferHistoryExpanded = !transferHistoryExpanded;
      paint();
      return;
    }
    const action = event.target.closest("[data-updates-action]");
    const cancel = event.target.closest("[data-transfer-cancel]");
    const retry = event.target.closest("[data-transfer-retry]");
    if (!action && !cancel && !retry) return;
    event.preventDefault();
    if (action) return void dispatch(action.dataset.updatesAction);
    const target = cancel || retry;
    target.disabled = true;
    try {
      await act(cancel ? "transferCancel" : "transferRetry", { id: cancel ? cancel.dataset.transferCancel : retry.dataset.transferRetry });
      await load(true);
      await tick();
    } catch (error) {
      notice(error.message, true);
    } finally {
      target.disabled = false;
    }
  });
  document.addEventListener("change", async (event) => {
    if (event.target.id === "updateChannel") return void saveChannel(event.target);
    const automatic = event.target.id === "automaticUpdates";
    if (!automatic) return;
    const control = event.target;
    if (automatic) savingAutomatic = true;
    control.disabled = true;
    try {
      await act(automatic ? "updateAutomatic" : "updateChannel", automatic ? {enabled: control.checked} : {channel: control.value});
      if (automatic && data) data.autoEnabled = control.checked;
      await load();
      await tick();
      notice(automatic ? control.checked ? "Автоматическое обновление включено." : "Автоматическое обновление выключено." : "Канал обновлений сохранён.");
    } catch (error) {
      notice(error.message, true);
      if (data) {
        if (automatic) control.checked = Boolean(data.autoEnabled);
        else control.value = data.channel;
      }
    } finally {
      if (automatic) savingAutomatic = false;
      control.disabled = false;
    }
  });
  window.UpdatesUI = { render, mount, dispatch, observe, isActive,
    handoffPending: () => progressData?.pending?.stage === "launching" };
})();
