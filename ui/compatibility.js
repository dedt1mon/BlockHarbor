"use strict";

const steamCompatibilityKey = (version, loader) => JSON.stringify([version, loader]);
let steamCompatibilityPrompt = null;

function showSteamCompatibilityWarning(report, variants) {
  // A separate dialog preserves the new-build form or modpack preview below it.
  if (steamCompatibilityPrompt) return Promise.resolve(false);
  const previousFocus = document.activeElement;
  const dialog = document.createElement("dialog");
  dialog.id = "steamCompatibilityDialog";
  dialog.setAttribute("aria-labelledby", "steamCompatibilityTitle");
  dialog.setAttribute("aria-describedby", "steamCompatibilityDescription");
  const uncertain = Boolean(report.unknown);
  const details = variants.slice(0, 6).map(item => `<span class="pill">Minecraft ${esc(item.version)} · ${esc(loaders[item.loader] || item.loader)}</span>`).join("");
  dialog.innerHTML = `<div class="dialog-top"><h2 id="steamCompatibilityTitle">Игра без подключения через Steam</h2><button type="button" class="icon-button" data-steam-cancel aria-label="Закрыть предупреждение">${ic("close")}</button></div>
    ${details ? `<div class="tags steam-compatibility-versions">${details}${variants.length > 6 ? `<span class="pill">Ещё ${variants.length - 6}</span>` : ""}</div>` : ""}
    <div id="steamCompatibilityDescription"><p>${uncertain
      ? "Для выбранного выпуска пока нельзя подтвердить встроенную поддержку Steam. Точную версию Minecraft и загрузчик проверим после чтения файла сборки."
      : "К сожалению, для этой версии Minecraft и выбранного загрузчика нет совместимого встроенного подключения через Steam."}</p>
    <p>Приносим извинения за неудобство. Вы можете продолжить без него: играть в одиночном режиме, на обычных серверах или по локальной сети, а также использовать другие способы подключения.</p>
    <p class="note">Отсутствие поддержки Steam не мешает установке и запуску самой игры.</p></div>
    <div class="dialog-actions"><button type="button" class="btn" data-steam-cancel>Отмена</button><button type="button" class="btn main" data-steam-continue>Продолжить без Steam</button></div>`;
  document.body.append(dialog);
  return new Promise(resolve => {
    let settled = false;
    const finish = accepted => {
      if (settled) return;
      settled = true;
      steamCompatibilityPrompt = null;
      if (dialog.open) dialog.close();
      moveNotice();
      dialog.remove();
      if (previousFocus?.isConnected) previousFocus.focus({preventScroll: true});
      resolve(accepted);
    };
    steamCompatibilityPrompt = dialog;
    for (const cancel of dialog.querySelectorAll("[data-steam-cancel]")) cancel.addEventListener("click", () => finish(false));
    dialog.querySelector("[data-steam-continue]").addEventListener("click", () => finish(true));
    dialog.addEventListener("cancel", event => {event.preventDefault(); finish(false);});
    dialog.addEventListener("close", () => finish(false), {once: true});
    dialog.showModal();
    moveNotice();
    dialog.querySelector(".dialog-actions [data-steam-cancel]").focus();
  });
}

async function confirmSteamCompatibility(profile, {report = null, acceptedPairs = new Set(), isCurrent = () => true} = {}) {
  const query = new URLSearchParams(profile.id ? {id: profile.id} : {version: profile.version, loader: profile.loader});
  const info = report || await api("/api/steam/compatibility?" + query);
  if (!isCurrent()) return null;
  const variants = (info.variants || [info]).filter(item => !item.supported && item.version && item.loader);
  const keys = variants.map(item => steamCompatibilityKey(item.version, item.loader));
  const needsWarning = !info.supported && !info.acknowledged && (info.unknown || !keys.length || keys.some(key => !acceptedPairs.has(key)));
  if (needsWarning && !(await showSteamCompatibilityWarning(info, variants))) return null;
  if (!isCurrent()) return null;
  return {allowWithoutSteam: !info.supported, acceptedPairs: new Set([...acceptedPairs, ...keys])};
}

function compatibilitySummary(report, manual = false) {
  function messages(title, rows, style = "") {
    if (!rows.length) return "";
    return (
      '<section class="dependency-group ' +
      style +
      '"><strong>' +
      esc(title) +
      "</strong><ul>" +
      rows.map((row) => "<li>" + esc(row) + "</li>").join("") +
      "</ul></section>"
    );
  }
  function describe(row) {
    const name = row.name || row.filename;
    return [
      name,
      row.version,
      row.filename && row.filename !== name ? row.filename : "",
    ]
      .filter(Boolean)
      .join(" · ");
  }
  const required = report.required || [];
  const pending = required.filter((r) => !r.installed).map(describe);
  const optional = report.optional || [];
  function extra(row) {
    return describe(row) + (row.installed ? " · уже в сборке" : "");
  }
  return (
    messages(
      "Конфликты в этой сборке",
      report.conflicts || [],
      "hard-conflict",
    ) +
    messages("Предупреждения", report.warnings || [], "soft-conflict") +
    messages(
      manual ? "Нужно добавить в сборку" : "Будут установлены вместе",
      pending,
      manual ? "soft-conflict" : "",
    ) +
    messages(
      "Обязательные зависимости уже в сборке",
      required.filter((r) => r.installed).map(describe),
    ) +
    messages(
      "Рекомендуемые дополнения",
      optional.filter((r) => r.recommended).map(extra),
    ) +
    messages(
      "Необязательные дополнения",
      optional.filter((r) => !r.recommended).map(extra),
    ) +
    messages("Встроены в выпуск", (report.embedded || []).map(describe))
  );
}

function releaseCompatibility(dialog, detail, profile, category) {
  let request;
  function cancel() {
    request?.abort();
    request = null;
  }
  dialog.addEventListener("close", cancel, { once: true });
  async function check(project, version) {
    cancel();
    const button = detail.querySelector("[data-install-project]");
    const area = detail.querySelector(".compatibility-report");
    if (!area) return;
    button.disabled = true;
    delete button.dataset.acknowledgement;
    area.removeAttribute("aria-busy");

    const current = new AbortController();
    request = current;
    button.textContent = "Установить в сборку";
    area.setAttribute("aria-busy", "true");
    area.textContent = "Проверяем зависимости и совместимость…";
    const params = new URLSearchParams({
      id: profile.id,
      kind: category,
      project: project.id,
      version: version.id,
      source: project.source || "all",
    });
    const active = () => request === current && dialog.open && area.isConnected;
    try {
      const info = await api("/api/catalog/check?" + params, {
        signal: current.signal,
      });
      if (!active()) return;
      area.innerHTML =
        compatibilitySummary(info) ||
        '<p class="compat-clear">Заявленных конфликтов и дополнительных зависимостей не найдено.</p>';
      button.disabled = info.blocked;
      if (info.reviewToken) button.dataset.acknowledgement = info.reviewToken;
      button.textContent = info.blocked
        ? "Установка недоступна"
        : info.reviewToken
          ? "Установить с предупреждением"
          : info.required.some((row) => !row.installed)
            ? "Установить с зависимостями"
            : version.installed ? "Проверить и восстановить зависимости" : "Установить в сборку";
    } catch (error) {
      if (!active()) return;
      area.innerHTML =
        '<p class="compat-error">' +
        esc(error.message) +
        '</p><button type="button" class="linkbtn">Повторить проверку</button>';
      area
        .querySelector("button")
        .addEventListener("click", () => check(project, version), {
          once: true,
        });
    } finally {
      if (active()) area.removeAttribute("aria-busy");
    }
  }
  return { cancel, check };
}

async function confirmManualMod(file, target) {
  const info = await putFile(file, "mods", target, true);
  const needsAttention =
    info.blocked ||
    info.conflicts.length ||
    info.warnings.length ||
    info.required.some((row) => !row.installed) ||
    info.optional.some((row) => row.recommended && !row.installed);
  if (!needsAttention) return true;
  const dialog = $("#dialog");
  const missing = info.required.some(row => !row.installed);
  modal(
    "Проверка мода",
    `<p class="manual-mod-name">${esc(file.name)}</p>
     <div class="compatibility-report">${compatibilitySummary(info, true)}</div>
     <div class="dialog-actions"><button type="button" class="btn" data-skip-mod>${info.blocked ? "Закрыть" : "Пропустить"}</button>
     ${info.blocked ? "" : `<button type="button" class="btn main" data-accept-mod>${missing ? "Подобрать зависимости" : "Добавить файл"}</button>`}</div>`,
  );
  return new Promise((resolve) => {
    let accepted = false, planned = false, token = "";
    dialog.addEventListener("close", () => resolve(accepted), { once: true });
    dialog.querySelector("[data-skip-mod]").onclick = closeDialog;
    const button = dialog.querySelector("[data-accept-mod]");
    if (!button) return;
    const area = dialog.querySelector(".compatibility-report");
    button.onclick = async () => {
      if (!missing) {
        accepted = true;
        closeDialog();
        return;
      }
      button.disabled = true;
      const stage = planned ? "install" : "plan";
      area.textContent = planned ? "Загружаем зависимости и проверяем мод…" : "Подбираем совместимые зависимости…";
      try {
        const result = await putFile(file, "mods", target, false, stage, token);
        if (!dialog.open || !button.isConnected) return;
        if (stage === "plan") {
          planned = !result.blocked;
          token = result.reviewToken || "";
          area.innerHTML = compatibilitySummary(result);
          button.textContent = planned ? "Установить мод с зависимостями" : "Повторить подбор";
        } else if (result.review) {
          token = result.review.token;
          area.innerHTML = compatibilitySummary({warnings:result.review.warnings});
          button.textContent = "Подтвердить установку";
        } else {
          accepted = "installed";
          closeDialog();
        }
      } catch (error) {
        if (button.isConnected) {
          planned = false;
          token = "";
          area.textContent = error.message;
          button.textContent = "Повторить подбор";
        }
      } finally {
        button.disabled = false;
      }
    };
  });
}

let shownCompatibilityReview = "";
function showStagedReview(job) {
  // Catalog jobs retain their explicit per-group review in BatchUI, even when
  // the user has collapsed its dialog to continue navigating the launcher.
  if (job.kind === "catalog-install" && window.BatchUI) return;
  const review = job.review;
  const dialog = $("#dialog");
  if (
    job.busy ||
    !review ||
    !review.request ||
    !review.token ||
    dialog.open ||
    job.id === shownCompatibilityReview
  )
    return;
  shownCompatibilityReview = job.id;
  notice("");
  const build = state.profiles.find((p) => p.id === review.request.id);
  modal(
    "Подтвердить установку",
    "<p>В скачанных файлах для сборки «" +
      esc(build?.name || "Minecraft") +
      "» обнаружены дополнительные условия.</p>" +
      '<div class="compatibility-report">' +
      compatibilitySummary({ warnings: review.warnings }) +
      "</div>" +
      '<div class="dialog-actions"><button type="button" class="btn" data-review-cancel>Отмена</button>' +
      '<button type="button" class="btn main" data-review-continue>Подтвердить установку</button></div>',
  );
  dialog.querySelector("[data-review-cancel]").onclick = closeDialog;
  const proceed = dialog.querySelector("[data-review-continue]");
  proceed.onclick = async () => {
    proceed.disabled = true;
    try {
      await runJob("installModrinth", {
        ...review.request,
        acknowledgement: review.token,
      });
    } catch (error) {
      proceed.disabled = false;
      notice(error.message, true);
    }
  };
}
