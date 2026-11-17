"use strict";

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
    if (version.installed) {
      area.replaceChildren();
      button.textContent = "Уже установлено";
      return;
    }
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
    });
    const active = () => request === current && dialog.open && area.isConnected;
    try {
      const info = await api("/api/modrinth/check?" + params, {
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
            : "Установить в сборку";
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
  modal(
    "Проверка мода",
    '<p class="manual-mod-name">' +
      esc(file.name) +
      "</p>" +
      '<div class="compatibility-report">' +
      compatibilitySummary(info, true) +
      '</div><div class="dialog-actions">' +
      '<button type="button" class="btn" data-skip-mod>' +
      (info.blocked ? "Закрыть" : "Пропустить") +
      "</button>" +
      (info.blocked
        ? ""
        : '<button type="button" class="btn main" data-accept-mod>Добавить файл</button>') +
      "</div>",
  );
  return new Promise((resolve) => {
    let accepted = false;
    dialog.addEventListener("close", () => resolve(accepted), { once: true });
    dialog.querySelector("[data-skip-mod]").onclick = closeDialog;
    const install = dialog.querySelector("[data-accept-mod]");
    if (install)
      install.onclick = () => {
        accepted = true;
        closeDialog();
      };
  });
}

let shownCompatibilityReview = "";
function showStagedReview(job) {
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
      '<button type="button" class="btn main" data-review-continue>Всё равно установить</button></div>',
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
