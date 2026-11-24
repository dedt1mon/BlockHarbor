"use strict";

// One bounded view of the current catalog operation. The server owns all state;
// closing this dialog neither cancels the worker nor guesses an installation result.
(() => {
  const labels = Object.freeze({
    queued: "Ожидает установки",
    checking: "Проверка совместимости",
    dependencies: "Проверка зависимостей",
    downloading: "Скачивание",
    installing: "Установка",
    installed: "Установлено",
    already_installed: "Уже установлено",
    incompatible: "Несовместимо",
    error: "Ошибка установки",
    cancelled: "Отменено",
    needs_review: "Нужно подтверждение",
  });
  const terminal = new Set(["installed", "already_installed", "incompatible", "error", "cancelled", "needs_review"]);
  const retryable = new Set(["incompatible", "error", "cancelled"]);
  let snapshot = null;
  let titles = new Map();
  let submitting = false;
  const name = item => titles.get(item.project) || item.title || item.project;
  const selection = item => ({project: item.project, kind: item.kind || "mods", version: item.version || null, source: item.source || "all"});

  function summary(job) {
    const items = job.items || [];
    const count = status => items.filter(item => item.status === status).length;
    return {
      selected: items.length,
      installed: count("installed"),
      alreadyInstalled: count("already_installed"),
      remaining: items.filter(item => !terminal.has(item.status)).length,
      failed: count("error") + count("incompatible"),
      cancelled: count("cancelled"),
      needsReview: count("needs_review"),
    };
  }

  function start(id, request) {
    const requested = request.items || [request];
    snapshot = {
      id, kind: "catalog-install", profileId: request.id, busy: true,
      items: requested.map(item => ({...selection(item), title: name(item), status: "queued", error: ""})),
      reviews: [],
    };
    open();
  }

  function observe(job) {
    if (job?.kind !== "catalog-install" || !Array.isArray(job.items)) return;
    snapshot = {...job, items: job.items.slice(0, 32), reviews: Array.isArray(job.reviews) ? job.reviews : []};
    update();
  }

  function open() {
    if (!snapshot) return;
    modal("Установка дополнений", '<section class="batch-panel" aria-label="Ход установки дополнений"></section>');
    update();
  }

  function update() {
    if (!snapshot) return;
    const toggle = document.querySelector("[data-batch-open]");
    if (toggle) {
      toggle.hidden = false;
      toggle.classList.toggle("is-active", Boolean(snapshot.busy));
      toggle.title = snapshot.busy ? "Установка дополнений выполняется" : "Результат установки дополнений";
      toggle.setAttribute("aria-label", toggle.title);
    }
    const host = document.querySelector(".batch-panel");
    if (!host) return;
    const rowsKey = JSON.stringify([snapshot.id, snapshot.items.map(item => item.project)]);
    if (host.dataset.rows !== rowsKey) {
      host.dataset.rows = rowsKey;
      host.innerHTML = `<p class="batch-context"></p><div class="batch-overview"><p class="batch-summary" role="status" aria-live="polite"></p><progress class="operation-progress" max="1" value="0" aria-label="Общий прогресс установки"></progress></div>
        <ol class="batch-items" aria-label="Дополнения">${snapshot.items.map(item => `<li class="batch-item" data-batch-project="${esc(item.project)}"><div class="batch-item-main"><strong class="batch-item-title"></strong><span class="batch-item-status"></span><p class="batch-item-error" hidden></p></div><button class="btn batch-retry" type="button" data-batch-retry="${esc(item.project)}" hidden>${ic("refresh")}Повторить</button></li>`).join("")}</ol>
        <div class="batch-reviews"></div><p class="batch-request-error" role="alert" hidden></p><div class="dialog-actions batch-actions"><button class="btn" type="button" data-batch-hide>Свернуть</button><button class="btn" type="button" data-batch-retry-failed hidden>${ic("refresh")}Повторить неустановленные</button><button class="btn danger" type="button" data-batch-cancel hidden>Отменить оставшиеся</button></div>`;
    }
    const profile = state?.profiles?.find(item => item.id === snapshot.profileId);
    host.querySelector(".batch-context").textContent = profile ? `${profile.name} · ${loaders[profile.loader]} · ${profile.version}` : "Выбранная сборка";
    const counts = summary(snapshot);
    host.querySelector(".batch-summary").textContent = [
      "Выбрано: " + counts.selected,
      "установлено: " + counts.installed,
      "уже в сборке: " + counts.alreadyInstalled,
      "осталось: " + counts.remaining,
      "не установлено: " + (counts.failed + counts.cancelled + counts.needsReview),
    ].join(" · ");
    const progress = host.querySelector("progress");
    progress.max = Math.max(1, counts.selected);
    progress.value = counts.selected - counts.remaining;
    progress.setAttribute("aria-valuetext", `${counts.selected - counts.remaining} из ${counts.selected} обработано`);
    for (const row of host.querySelectorAll("[data-batch-project]")) {
      const item = snapshot.items.find(value => value.project === row.dataset.batchProject);
      row.dataset.status = item.status;
      row.querySelector(".batch-item-title").textContent = name(item);
      row.querySelector(".batch-item-status").textContent = labels[item.status] || "Подготовка…";
      const error = row.querySelector(".batch-item-error");
      error.textContent = item.error || "";
      error.hidden = !item.error;
      const retry = row.querySelector("[data-batch-retry]");
      retry.hidden = Boolean(snapshot.busy) || !retryable.has(item.status);
      retry.disabled = submitting;
      retry.setAttribute("aria-label", "Повторить установку «" + name(item) + "»");
    }
    const reviews = host.querySelector(".batch-reviews");
    const reviewsKey = JSON.stringify(snapshot.reviews);
    if (reviews.dataset.items !== reviewsKey) {
      reviews.dataset.items = reviewsKey;
      reviews.innerHTML = snapshot.reviews.map((review, index) => `<section class="batch-review"><h3>Подтверждение совместимости</h3><p>${(review.request?.items || []).map(item => esc(name(item))).join(", ")}</p><ul>${(review.warnings || []).map(warning => `<li>${esc(typeof warning === "string" ? warning : warning.message || warning.title || warning.code || "Совместимость требует проверки")}</li>`).join("")}</ul><button class="btn" type="button" data-batch-review="${index}">Установить с предупреждением</button></section>`).join("");
    }
    for (const button of reviews.querySelectorAll("button")) button.disabled = Boolean(snapshot.busy) || submitting;
    const retry = host.querySelector("[data-batch-retry-failed]");
    retry.hidden = Boolean(snapshot.busy) || !snapshot.items.some(item => retryable.has(item.status));
    retry.disabled = submitting;
    const cancel = host.querySelector("[data-batch-cancel]");
    cancel.hidden = !snapshot.busy || !snapshot.cancellable;
    cancel.disabled = submitting || Boolean(snapshot.cancelRequested);
    host.querySelector("[data-batch-hide]").textContent = snapshot.busy ? "Свернуть" : "Закрыть";
  }

  async function retry(request, acknowledgement = "") {
    if (submitting || snapshot?.busy) return;
    submitting = true;
    update();
    try {
      await runJob("installModrinth", {...request, acknowledgement});
    } catch (error) {
      const node = document.querySelector(".batch-request-error");
      if (node) { node.hidden = false; node.textContent = error.message; }
      else notice(error.message, true);
    } finally {
      submitting = false;
      update();
    }
  }

  document.addEventListener("click", async event => {
    const button = event.target.closest("button");
    if (!button || button.disabled) return;
    if (button.hasAttribute("data-batch-open")) { open(); return; }
    if (button.hasAttribute("data-batch-hide")) { closeDialog(); return; }
    if (!snapshot) return;
    if (button.hasAttribute("data-batch-retry")) {
      const item = snapshot.items.find(item => item.project === button.dataset.batchRetry);
      if (item && retryable.has(item.status)) await retry({id: snapshot.profileId, items: [selection(item)]});
    } else if (button.hasAttribute("data-batch-retry-failed")) {
      await retry({id: snapshot.profileId, items: snapshot.items.filter(item => retryable.has(item.status)).map(selection)});
    } else if (button.hasAttribute("data-batch-review")) {
      const review = snapshot.reviews[Number(button.dataset.batchReview)];
      if (review?.request && review.token) await retry(review.request, review.token);
    } else if (button.hasAttribute("data-batch-cancel") && snapshot.cancellable) {
      button.disabled = true;
      try { await act("cancelCatalogInstall", {job: snapshot.id}); await tick(); }
      catch (error) { notice(error.message, true); button.disabled = false; }
    }
  });
  window.BatchUI = {start, observe, open, labels, summary, rememberTitles(items) { titles = new Map(items.slice(0, 32).map(item => [item.project, item.title])); }};
})();
