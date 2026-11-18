/* Release and transfer UI. Loaded after app.js, using the shared API and theme. */
(() => {
  "use strict";
  let data = null;
  let timer = 0;
  let fetching = false;
  let lastTransferMarkup = "";
  let lastReleaseMarkup = "";
  const labels = {
    queued: "В очереди", downloading: "Загрузка", verifying: "Проверка",
    completed: "Готово", cancelled: "Отменено", failed: "Ошибка", interrupted: "Прервано",
  };
  const button = (action, text, icon, extra = "", disabled = false) =>
    `<button class="btn ${extra}" type="button" data-updates-action="${esc(action)}" ${disabled ? "disabled" : ""}>${ic(icon)}${esc(text)}</button>`;
  const formatDate = (seconds) => seconds ? new Date(seconds * 1000).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }) : "Ещё не проверяли";

  function render({embedded = false} = {}) {
    const actions = button("updateCheck", "Проверить обновления", "refresh");
    const heading = embedded
      ? `<div class="settings-updates-head"><div><h2>Загрузки и обновления</h2><p class="muted">Обновление BlockHarbor и очередь скачиваемых файлов.</p></div>${actions}</div>`
      : head("Загрузки и обновления", actions, "Версии лаунчера, очередь файлов и результаты проверки.");
    return heading +
      `<div id="updatesScreen"><section class="panel updater-channel"><div><h2>Канал обновлений</h2><p class="muted">Стабильный — для обычной игры. Тестовый — для ранних версий BlockHarbor.</p></div><label>Канал<select id="updateChannel" aria-label="Канал обновлений"><option value="stable">Стабильный</option><option value="test">Тестовый</option></select></label></section><div id="updateRelease" aria-live="polite">${data ? releaseMarkup() : '<div class="empty">Получаем состояние обновлений…</div>'}</div><section class="panel transfer-panel"><div class="transfer-heading"><div><h2>Загрузки</h2><p class="muted">Файл появляется в сборке после проверки контрольной суммы.</p></div>${button("transferClear", "Очистить историю", "trash")}</div><div id="transferList">${data ? transfersMarkup() : '<p class="muted">Загружаем очередь…</p>'}</div></section></div>`;
  }

  function releaseMarkup() {
    if (!data) return "";
    const release = data.release;
    const busy = data.transfers?.tasks.some((task) => task.category === "launcher-update" && ["queued", "downloading", "verifying"].includes(task.state));
    let actions = "";
    let heading = `Установлен BlockHarbor ${esc(data.current)}`;
    let body = `<p class="muted">Последняя проверка: ${esc(formatDate(data.checkedAt))}.</p>`;
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
      body += `<div class="update-pending" role="status">${ic("refresh")}<div><strong>${stage === "queued" ? "Установка запланирована" : stage === "preparing" ? "Сохраняем предыдущую версию" : "Открываем установщик"}</strong><p>${stage === "queued" ? "Закрой другие окна BlockHarbor. После выхода из всех игр и завершения операций откроется установщик." : "BlockHarbor закроется после подготовки помощника установки."}</p></div></div>`;
      if (stage !== "launching") actions += button("updateCancelInstall", "Отменить установку", "close");
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
    return liveJob + tasks.map((task) => {
      const running = ["queued", "downloading", "verifying"].includes(task.state);
      const total = Number(task.total);
      const bytes = Number(task.bytes) || 0;
      const progress = running ? `<progress ${total > 0 ? `max="${total}" value="${Math.min(bytes, total)}"` : ""} aria-label="Загрузка ${esc(task.title)}"></progress>` : "";
      const amount = total > 0 ? `${fmt(bytes)} / ${fmt(total)}` : bytes ? fmt(bytes) : "";
      const speed = task.state === "downloading" && task.rate > 0 ? `${fmt(task.rate)}/с` : "";
      const actions = running ? `<button class="icon-button" data-transfer-cancel="${esc(task.id)}" aria-label="Отменить загрузку ${esc(task.title)}">${ic("close")}</button>` : task.retryable && task.state !== "completed" ? `<button class="btn" data-transfer-retry="${esc(task.id)}">${ic("refresh")}Повторить</button>` : "";
      return `<article class="transfer-row transfer-${esc(task.state)}"><div class="transfer-icon">${ic(task.state === "completed" ? "check" : task.state === "failed" ? "close" : "download")}</div><div class="transfer-info"><div class="transfer-title"><strong>${esc(task.title)}</strong><span class="pill">${esc(labels[task.state] || task.state)}</span></div>${progress}<div class="transfer-metrics"><span>${esc(amount)}${speed ? " · " + esc(speed) : ""}${task.resumed ? " · продолжена" : ""}</span><span>${esc(task.host || "")}</span></div><p class="transfer-message">${esc(task.message || "")}${!running && !task.retryable && ["failed", "cancelled", "interrupted"].includes(task.state) ? " Повтори установку из исходного окна." : ""}</p></div><div class="transfer-actions">${actions}</div></article>`;
    }).join("");
  }

  function paint() {
    const screen = document.querySelector("#updatesScreen");
    if (!screen || !data) return;
    const channel = document.querySelector("#updateChannel");
    if (channel && document.activeElement !== channel) channel.value = data.channel;
    const release = releaseMarkup();
    if (release !== lastReleaseMarkup) {
      const open = document.querySelector(".update-notes")?.open;
      document.querySelector("#updateRelease").innerHTML = release;
      if (open && document.querySelector(".update-notes")) document.querySelector(".update-notes").open = true;
      lastReleaseMarkup = release;
    }
    const transfers = transfersMarkup();
    if (transfers !== lastTransferMarkup) {
      const focus = document.activeElement?.closest("[data-transfer-cancel], [data-transfer-retry]");
      const focusedId = focus?.dataset.transferCancel || focus?.dataset.transferRetry;
      const focusKind = focus?.dataset.transferCancel ? "cancel" : "retry";
      document.querySelector("#transferList").innerHTML = transfers;
      if (focusedId) document.querySelector(`[data-transfer-${focusKind}="${CSS.escape(focusedId)}"]`)?.focus({ preventScroll: true });
      lastTransferMarkup = transfers;
    }
  }

  async function load(silent = false) {
    if (fetching || !document.querySelector("#updatesScreen")) return;
    fetching = true;
    try {
      data = await api("/api/feature?action=updateState");
      paint();
    } catch (error) {
      if (!silent) notice(error.message, true);
    } finally {
      fetching = false;
      clearTimeout(timer);
      if (document.querySelector("#updatesScreen")) timer = setTimeout(() => load(true), 1500);
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
        await act(action);
        if (action === "updateDownload") notice("Обновление добавлено в очередь загрузок.");
        if (action === "updateCancelInstall") notice("Отложенная установка отменена.");
      }
      await load(true);
    } catch (error) {
      notice(error.message, true);
    }
    return true;
  }

  document.addEventListener("click", async (event) => {
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
    } catch (error) {
      notice(error.message, true);
    } finally {
      target.disabled = false;
    }
  });
  document.addEventListener("change", async (event) => {
    if (event.target.id !== "updateChannel") return;
    try {
      await act("updateChannel", { channel: event.target.value });
      await load();
      notice("Канал обновлений сохранён. Проверь доступные версии.");
    } catch (error) {
      notice(error.message, true);
      if (data) event.target.value = data.channel;
    }
  });
  window.UpdatesUI = { render, mount, dispatch };
})();
