/* Build recovery and installed-content manager. Uses the authenticated local API. */
"use strict";
window.BuildCareUI = (() => {
  const cache = new Map(), pending = new Map(), selected = new Map(), queries = new Map();
  let worldsRequest = 0;
  const text = (v) => esc(v ?? "");
  const date = (v) => new Date(v * 1000).toLocaleString("ru-RU", {dateStyle: "medium", timeStyle: "short"});
  const idFor = (pid, category) => pid + ":" + category;
  const chosen = (pid, category) => {
    const key = idFor(pid, category);
    if (!selected.has(key)) selected.set(key, new Set());
    return selected.get(key);
  };
  const signature = () => JSON.stringify([state?.active, state?.contents, state?.job?.id, state?.job?.busy]);
  const feature = (action, data) => api("/api/feature?" + new URLSearchParams({action, ...data}));
  const actionButton = (action, label, icon, extra = "") => `<button type="button" class="btn" data-care="${action}" ${extra}>${icon ? ic(icon) : ""}${label}</button>`;
  async function load(pid, force = false) {
    const sig = signature();
    if (!force && cache.get(pid)?.signature === sig) return cache.get(pid).data;
    if (pending.has(pid)) return pending.get(pid);
    const request = feature("careState", {id: pid}).then(data => {
      cache.set(pid, {data, signature: sig});
      return data;
    }).finally(() => pending.delete(pid));
    pending.set(pid, request);
    return request;
  }
  function rowMarkup(row, pid, category) {
    const key = idFor(pid, category), selectedNames = chosen(pid, category);
    const query = (queries.get(key) || "").toLocaleLowerCase("ru-RU");
    const match = !query || [row.title, row.name, row.description, row.version].join(" ").toLocaleLowerCase("ru-RU").includes(query);
    const enabled = row.enabled !== false;
    return `<div class="care-row ${enabled ? "" : "is-disabled"}" data-care-row="${text(row.name)}" ${match ? "" : "hidden"}>
      <input type="checkbox" class="care-select" aria-label="Выбрать ${text(row.title || row.name)}" data-care-select="${text(row.name)}" ${selectedNames.has(row.name) ? "checked" : ""} ${row.managed ? "disabled" : ""}/>
      <span class="installed-icon" aria-hidden="true" data-image="${text(row.icon || "")}">${ic(kinds[category]?.[1] || "cube")}</span>
      <button type="button" class="care-title" data-care-detail="${text(row.name)}"><strong>${text(row.title || row.name)}</strong><span>${text(row.description || row.filename || row.name)}</span></button>
      <span class="care-version">${text(row.version || "Локальный файл")}${row.pinned ? '<small title="Пропускается при обновлении">Версия закреплена</small>' : ""}</span>
      <span class="care-size">${fmt(row.size)}</span>
      <div class="care-row-actions">${row.managed ? '<span class="pill">Встроено</span>' : `<label class="care-switch" title="${enabled ? "Отключить" : "Включить"} ${text(row.title || row.name)}"><input type="checkbox" role="switch" aria-label="${enabled ? "Отключить" : "Включить"} ${text(row.title || row.name)}" data-care-toggle="${text(row.name)}" ${enabled ? "checked" : ""}/><span aria-hidden="true"></span><b class="sr-only">${enabled ? "Включён" : "Отключён"}</b></label><button type="button" class="btn care-item-delete" data-care-remove="${text(row.name)}" aria-label="Удалить ${text(row.title || row.name)}" title="Удалить">${ic("trash")}</button>`}</div>
    </div>`;
  }
  function content(profile, category, fallbackRows = []) {
    const data = cache.get(profile.id)?.data;
    const rows = data?.contents[category] || fallbackRows.map(r => ({...r, enabled: true, title: r.name}));
    const key = idFor(profile.id, category), query = queries.get(key) || "";
    return `<section class="care-content" data-care-content data-care-pid="${text(profile.id)}" data-care-kind="${text(category)}">
      <div class="care-content-head"><label class="care-search">${ic("search")}<input class="field" type="search" data-care-search placeholder="Найти среди установленных…" value="${text(query)}" aria-label="Найти установленное дополнение"/></label></div>
      <div class="care-bulk"><label><input type="checkbox" data-care-all/> Выбрать видимые</label><div class="care-bulk-actions"><span data-care-count>${chosen(profile.id, category).size} выбрано</span>
      ${actionButton("enable", "Включить", "check")}${actionButton("disable", "Отключить", "close")}${actionButton("remove", "Удалить", "trash")}</div></div>
      <div class="care-list">${rows.map(r => rowMarkup(r, profile.id, category)).join("") || '<div class="empty">Пока нет установленных дополнений.</div>'}</div>
      <p class="care-footnote">Отключённые файлы остаются в сборке. Перед изменениями автоматически сохраняется точка восстановления.</p>
    </section>`;
  }
  function context(element) {
    const scope = element.closest("[data-care-pid]");
    return {pid: scope?.dataset.carePid || active()?.id, category: scope?.dataset.careKind || kind};
  }
  async function repaint(pid) {
    cache.delete(pid);
    await load(pid, true);
    await refresh();
    if (active()?.id === pid) render();
  }
  async function waitJob(action, data, after) {
    await runJob(action, data);
    const expected = lastJob;
    for (;;) {
      await new Promise(resolve => setTimeout(resolve, 750));
      const job = await api("/api/job");
      if (job.id !== expected) break;
      if (!job.busy) {
        if (!job.ok) throw new Error(job.message || "Операция не удалась.");
        await repaint(data.id);
        if (after) await after();
        return;
      }
    }
  }
  async function change(pid, category, names, mode, force = false) {
    if (!names.length) throw new Error("Сначала выбери дополнения.");
    const action = mode === "remove" ? "careRemove" : "careToggle";
    const payload = {id: pid, kind: category, names, enabled: mode === "enable", force};
    const result = await act(action, payload);
    if (result.review) {
      modal("Проверка зависимостей", `<p>${text(result.message)}</p><ul class="care-warnings">${[...(result.dependents || []).map(v => "Зависит от выбранных файлов: " + v), ...(result.warnings || [])].map(v => `<li>${text(v)}</li>`).join("")}</ul><div class="actions">${actionButton("cancel", "Отмена")}${actionButton("confirm-change", "Всё равно продолжить", "check")}</div>`);
      document.querySelector('[data-care="confirm-change"]').onclick = async () => {
        try { closeDialog(); await change(pid, category, names, mode, true); } catch (error) { notice(error.message, true); }
      };
      return false;
    }
    chosen(pid, category).clear();
    notice(result.message || "Изменения сохранены.");
    await repaint(pid);
    return true;
  }
  async function details(pid, category, name) {
    const data = await feature("careDetails", {id: pid, kind: category, name});
    modal(data.title, `<div class="care-detail"><p>${text(data.description || "Автор не добавил описание в локальный файл.")}</p>
      <dl><dt>Файл</dt><dd>${text(data.name)}</dd><dt>Версия</dt><dd>${text(data.version || "Не указана")}</dd><dt>Состояние</dt><dd>${data.enabled ? "Включён" : "Отключён"}${data.pinned ? " · закреплён" : ""}</dd><dt>Размер</dt><dd>${fmt(data.size)}</dd></dl>
      <h3>Зависимости</h3>${data.requires.length ? `<ul>${data.requires.map(d => `<li><strong>${text(d.id)}</strong> ${text(d.versions.join(" / "))}<span class="care-detail-hint">${d.platform ? "Компонент игры / загрузчика" : d.installed.length ? "Найдено: " + text(d.installed.join(", ")) : d.relation === "required" ? "Обязательный компонент не найден" : "Необязательный компонент"}</span></li>`).join("")}</ul>` : '<p class="muted">В метаданных файла зависимости не указаны.</p>'}
      <h3>Используется другими модами</h3><p>${data.dependents.length ? data.dependents.map(d => text(d.title)).join(", ") : "Обязательных зависимостей от этого файла не найдено."}</p>
      <p class="care-footnote">Проверка основана на метаданных автора. Отсутствие предупреждений не гарантирует совместимость каждого сочетания модов.</p></div>`);
  }
  async function history(pid, world = "") {
    const data = await load(pid, true);
    const points = data.snapshots.filter(s => !world || s.world === world);
    modal(world ? "Копии мира · " + world : "Восстановление сборки", `<div data-care-pid="${text(pid)}" class="care-recovery">
      <p>Точка сборки сохраняет дополнения, конфигурацию и настройки. Резервные копии миров хранятся отдельно. Перед откатом текущее состояние тоже сохраняется.</p>
      <div class="care-points" tabindex="0" aria-label="Точки восстановления">${points.length ? points.map(s => `<article class="care-point"><div><strong>${text(s.label)}</strong><span>${s.scope === "world" ? "Мир · " + text(s.world) : "Сборка"} · ${date(s.created)} · ${fmt(s.size)}</span></div><button type="button" class="btn" data-care-restore="${text(s.id)}" data-care-scope="${text(s.scope)}" data-care-world="${text(s.world)}">Восстановить</button></article>`).join("") : '<div class="empty">Копий пока нет. Первая появится при изменении сборки.</div>'}</div>
      <details class="care-history"><summary>История изменений</summary>${data.history.map(h => `<p><strong>${text(h.action)}</strong><time>${date(h.created)}</time><span>${text(h.detail)}</span></p>`).join("") || '<p>История пока пуста.</p>'}</details>
      <details class="care-retention"><summary>Хранение копий · занято ${fmt(data.recoveryBytes)}</summary><p>Сохранять последние копии отдельно для сборки и каждого мира. Общий лимит применяется ко всем сборкам; старые точки удаляются автоматически.</p><div class="care-retention-fields"><label>Копий в истории<input class="field" type="number" min="1" max="100" data-care-retention-count value="${data.retention.count}"/></label><label>Общий лимит, МБ<input class="field" type="number" min="128" max="102400" data-care-retention-budget value="${data.retention.budgetMiB}"/></label>${actionButton("retention", "Сохранить лимиты", "check")}</div><p class="care-footnote">Только что созданная копия сохраняется, даже если сама превышает лимит.</p></details>
    </div>`, {headerActions: actionButton("snapshot", "Создать точку", "plus", `data-care-pid="${text(pid)}"`)});
  }
  function worlds(profile = active()) {
    if (!profile) return '<div class="empty">Создай сборку, чтобы управлять её мирами.</div>';
    return `<section class="care-worlds" data-care-pid="${text(profile.id)}" data-care-worlds><div class="care-worlds-head"><div><h2>Миры · ${text(profile.name)}</h2><p>Изменённые миры сохраняются автоматически после выхода из Minecraft. Дата показывает последнее изменение мира.</p></div><div class="actions">${actionButton("backup-all", "Скопировать все миры", "copy")}${actionButton("history", "Все копии", "refresh")}</div></div><div data-care-world-list><div class="empty">Читаем список миров…</div></div></section>`;
  }
  async function mountWorlds(container) {
    const pid = container.dataset.carePid, request = ++worldsRequest;
    const [data, care] = await Promise.all([feature("careWorlds", {id: pid}), load(pid)]);
    if (request !== worldsRequest || !container.isConnected) return;
    container.querySelector("[data-care-world-list]").innerHTML = data.worlds.map(w => {
      const copies = care.snapshots.filter(s => s.scope === "world" && s.world === w.name);
      return `<article class="care-world"><div>${ic("layers")}<strong>${text(w.name)}</strong><span>${fmt(w.size)} · ${date(w.lastPlayed)}</span><small>${copies.length ? "Копий: " + copies.length + " · последняя " + date(copies[0].created) : "Резервных копий пока нет"}</small></div><div class="actions"><button type="button" class="btn" data-care-backup-world="${text(w.name)}">${ic("plus")}Создать копию</button><button type="button" class="btn" data-care-world-history="${text(w.name)}" ${copies.length ? "" : "disabled"}>Восстановить</button></div></article>`;
    }).join("") || '<div class="empty">Сохранённых миров пока нет. Здесь появятся миры, созданные в этой сборке.</div>';
  }
  function mount() {
    const container = document.querySelector("[data-care-content]");
    if (container) {
      const pid = container.dataset.carePid;
      if (cache.get(pid)?.signature !== signature() && !pending.has(pid)) {
        load(pid).then(() => { if (document.querySelector('[data-care-content]')?.dataset.carePid === pid) render(); }).catch(error => notice(error.message, true));
      }
    }
    for (const world of document.querySelectorAll("[data-care-worlds]")) {
      if (!world.dataset.loading) {
        world.dataset.loading = "1";
        mountWorlds(world).catch(error => { if (world.isConnected) world.querySelector("[data-care-world-list]").textContent = error.message; });
      }
    }
  }
  async function handle(action, element) {
    const {pid, category} = context(element);
    if (action === "cancel") return closeDialog();
    if (!pid) throw new Error("Сначала выбери сборку.");
    if (action === "history") return history(pid);
    if (action === "snapshot") { closeDialog(); return waitJob("careSnapshot", {id: pid}, () => history(pid)); }
    if (action === "backup-all") { closeDialog(); return waitJob("careBackupWorlds", {id: pid}); }
    if (action === "retention") {
      const result = await act("careRetention", {id: pid, count: Number(document.querySelector("[data-care-retention-count]").value), budgetMiB: Number(document.querySelector("[data-care-retention-budget]").value)});
      notice(result.message); return history(pid);
    }
    if (["enable", "disable", "remove"].includes(action)) {
      const names = [...chosen(pid, category)];
      if (!names.length) throw new Error("Сначала выбери дополнения.");
      if (action === "remove") {
        return confirmRemoval(pid, category, names);
      }
      return change(pid, category, names, action);
    }
  }
  function confirmRemoval(pid, category, names) {
    modal(names.length === 1 ? "Удалить дополнение?" : "Удалить выбранные дополнения?", `<p>Будет удалено файлов: ${names.length}. Перед изменением сохранится точка восстановления.</p><ul class="care-removal-files">${names.map(name => `<li>${text(name)}</li>`).join("")}</ul><div class="actions">${actionButton("cancel", "Отмена")}<button type="button" class="btn danger" id="careConfirmDelete">Удалить</button></div>`, {contentSized: true});
    document.querySelector("#careConfirmDelete").onclick = async () => { try { closeDialog(); await change(pid, category, names, "remove"); } catch(error) { notice(error.message, true); } };
  }
  document.addEventListener("input", event => {
    if (!event.target.matches("[data-care-search]")) return;
    const {pid, category} = context(event.target), query = event.target.value.toLocaleLowerCase("ru-RU");
    queries.set(idFor(pid, category), event.target.value);
    const data = cache.get(pid)?.data?.contents[category] || [];
    for (const row of document.querySelectorAll("[data-care-row]")) {
      const item = data.find(v => v.name === row.dataset.careRow);
      row.hidden = ![item?.title, item?.name, item?.description, item?.version, row.textContent].join(" ").toLocaleLowerCase("ru-RU").includes(query);
    }
  });
  document.addEventListener("change", async event => {
    const target = event.target;
    if (!target.matches("[data-care-select], [data-care-all], [data-care-toggle]")) return;
    const {pid, category} = context(target), selection = chosen(pid, category);
    if (target.matches("[data-care-toggle]")) {
      target.disabled = true;
      try { if (!await change(pid, category, [target.dataset.careToggle], target.checked ? "enable" : "disable")) target.checked = !target.checked; }
      catch (error) { target.checked = !target.checked; notice(error.message, true); }
      finally { target.disabled = false; }
      return;
    }
    const inputs = target.matches("[data-care-all]") ? [...document.querySelectorAll("[data-care-row]:not([hidden]) [data-care-select]:not(:disabled)")] : [target];
    for (const input of inputs) {
      input.checked = target.checked;
      if (target.checked) selection.add(input.dataset.careSelect); else selection.delete(input.dataset.careSelect);
    }
    const count = document.querySelector("[data-care-count]");
    if (count) count.textContent = selection.size + " выбрано";
  });
  document.addEventListener("click", async event => {
    const element = event.target.closest("[data-care], [data-care-detail], [data-care-remove], [data-care-restore], [data-care-backup-world], [data-care-world-history]");
    if (!element || element.dataset.care === "confirm-change") return;
    event.preventDefault();
    const {pid, category} = context(element);
    try {
      if (element.dataset.care) await handle(element.dataset.care, element);
      else if (element.hasAttribute("data-care-detail")) await details(pid, category, element.dataset.careDetail);
      else if (element.hasAttribute("data-care-remove")) confirmRemoval(pid, category, [element.dataset.careRemove]);
      else if (element.hasAttribute("data-care-backup-world")) { closeDialog(); await waitJob("careWorldBackup", {id: pid, world: element.dataset.careBackupWorld}); }
      else if (element.hasAttribute("data-care-world-history")) await history(pid, element.dataset.careWorldHistory);
      else if (element.hasAttribute("data-care-restore")) {
        const sid = element.dataset.careRestore, world = element.dataset.careWorld;
        modal("Восстановить " + (world ? "мир?" : "сборку?"), `<p>${world ? "Содержимое мира «" + text(world) + "» будет заменено выбранной копией." : "Будут восстановлены дополнения, конфигурация и настройки запуска. Миры не меняются."} Текущее состояние будет сохранено перед заменой.</p>`, {
          headerActions: '<button type="button" class="btn main" id="careConfirmRestore">Восстановить</button>',
          contentSized: true,
        });
        document.querySelector("#careConfirmRestore").onclick = async () => { try { closeDialog(); await waitJob("careRestore", {id: pid, snapshot: sid}); } catch(error) { notice(error.message, true); } };
      }
    } catch (error) { notice(error.message, true); }
  });
  return {content, mount, worlds, invalidate(pid) { cache.delete(pid); }, dispatch: async () => false};
})();
