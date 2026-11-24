/* Local preferences, diagnostics, skins and screenshots. */
(() => {
  let buildQuery = "", onlyFavorites = false;
  let report = "";
  let diagnosticSession = null;
  const galleries = new Map();
  const wardrobeDeletes = new Set();
  let galleryRequest = 0;
  let notificationRequest = 0;
  let notificationObservation = 0;
  const query = (action, p = {}) => api("/api/feature?" + new URLSearchParams({action, ...p}));
  const date = (v) => v ? new Date(v * 1000).toLocaleString("ru-RU", {dateStyle:"short",timeStyle:"short"}) : "—";
  const media = (digest) => /^[a-f0-9]{64}$/.test(digest || "") ? "/api/media/" + digest + ".png" : "";
  const expButton = (action, text, icon = "settings", extra = "") => `<button type="button" class="btn" data-experience="${action}" ${extra}>${ic(icon)}${text}</button>`;
  const download = (text, name, type = "text/plain;charset=utf-8") => {
    const url = URL.createObjectURL(new Blob(["\uFEFF", text], {type}));
    const a = document.createElement("a"); a.href = url; a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  };
  function preferences() {
    const p = state.experience?.preferences || {};
    document.documentElement.style.fontSize = (15 * (p.scale || 100) / 100) + "px";
    document.body.classList.toggle("reduced-motion", Boolean(p.reducedMotion));
  }
  function observeNotifications(value) {
    if (!Number.isFinite(value)) return;
    notificationObservation++;
    const unread = Math.max(0, Math.trunc(value));
    const bell = document.querySelector(".notification-bell");
    if (bell) {
      bell.querySelector(".notification-dot").hidden = unread === 0;
      const label = "История уведомлений" + (unread ? " · непрочитанных: " + unread : "");
      bell.setAttribute("aria-label", label);
      bell.title = label;
    }
    if (state) {
      state.notificationUnread = unread;
      if (state.experience) state.experience.notificationUnread = unread;
    }
    const refresh = document.querySelector("[data-notifications-refresh]");
    if (refresh) refresh.hidden = unread === 0;
  }
  async function notifications() {
    const request = ++notificationRequest;
    const requestedAt = notificationObservation;
    modal("История уведомлений", '<section class="notification-panel"><p role="status">Загружаем уведомления…</p></section>', {
      headerActions: expButton("clearNotifications", "Очистить", "trash", "disabled"),
    });
    const host = document.querySelector(".notification-panel");
    const current = () => request === notificationRequest && host.isConnected && $("#dialog").open;
    let data;
    try { data = await query("experienceNotifications"); }
    catch (error) {
      if (current()) {
        host.innerHTML = `<p class="notification-read-error" role="alert">${esc(error.message)}</p><button class="btn" type="button" data-notifications-retry>${ic("refresh")}Повторить</button>`;
        host.querySelector("[data-notifications-retry]").onclick = () => notifications().catch(e => notice(e.message, true));
      }
      return;
    }
    if (!current()) return;
    const items = data.items || [];
    host.innerHTML = `<div class="notification-history" aria-label="Уведомления">${items.length ? items.map(n => `<article class="notification-item ${esc(n.level)}${n.read ? "" : " unread"}"><div class="notification-heading"><time datetime="${new Date(n.created * 1000).toISOString()}">${date(n.created)}</time>${n.read ? "" : '<span class="notification-new">Новое</span>'}</div><p>${esc(n.message)}</p></article>`).join("") : '<p class="note">История пока пуста.</p>'}</div><div class="dialog-actions"><button class="btn" type="button" data-notifications-refresh hidden>${ic("refresh")}Показать новые</button></div><p class="notification-read-error" role="status" hidden></p>`;
    document.querySelector('#dialogHeaderActions [data-experience="clearNotifications"]').disabled = !items.length;
    host.querySelector("[data-notifications-refresh]").onclick = () => notifications().catch(e => notice(e.message, true));
    if (notificationObservation === requestedAt) observeNotifications(data.unread);
    else observeNotifications(state.notificationUnread);
    if (data.lastId) {
      const readStartedAt = notificationObservation;
      let read;
      try {
        // Only the snapshot actually rendered above is acknowledged. A newer server
        // event arriving in parallel stays unread and keeps the bell indicator.
        read = await act("experienceReadNotifications", {throughId: data.lastId});
      } catch (error) {
        if (current()) {
          const message = host.querySelector(".notification-read-error");
          message.hidden = false;
          message.textContent = "Не удалось сохранить прочтение: " + error.message;
        }
        return;
      }
      if (notificationObservation === readStartedAt) {
        observeNotifications(read.unread);
      } else {
        // A poll or another history request supplied a newer observation while
        // the ACK was in flight. Its older count must not hide a new event.
        const refreshStartedAt = notificationObservation;
        try {
          const latest = await query("experienceState");
          if (notificationObservation === refreshStartedAt) observeNotifications(latest.notificationUnread);
        } catch (error) {
          if (current()) {
            const message = host.querySelector(".notification-read-error");
            message.hidden = false;
            message.textContent = "Не удалось обновить счётчик уведомлений: " + error.message;
          }
        }
      }
    }
  }
  function filterBar() {
    return `<div class="build-filter"><label class="search-field">${ic("search")}<input class="field" id="buildSearch" placeholder="Найти сборку…" value="${esc(buildQuery)}" aria-label="Поиск сборок"></label><label class="inline-check"><input id="onlyFavorites" type="checkbox" ${onlyFavorites?"checked":""}>Избранное</label></div><p id="buildFilterEmpty" class="note" hidden>По этим условиям сборки не найдены.</p>`;
  }
  function filterRows() {
    let count = 0;
    for (const row of document.querySelectorAll(".build-row[data-profile]")) {
      const p = state.profiles.find(p => p.id === row.dataset.profile);
      if (!p) continue;
      const matches = (!buildQuery || [p.name,p.version,p.loader,p.description,p.group_name].join(" ").toLowerCase().includes(buildQuery.toLowerCase())) && (!onlyFavorites || p.favorite);
      row.hidden = !matches; if (matches) count++;
    }
    if ($("#buildFilterEmpty")) $("#buildFilterEmpty").hidden = count > 0 || !state.profiles.length;
  }
  function settingsPanel() {
    const p = state.experience?.preferences || {};
    return `<section class="panel stack settings-card"><h3>Оформление</h3><label>Масштаб текста<select class="field" id="uiScale">${[85,90,100,110,120].map(v=>`<option value="${v}" ${(p.scale||100)===v?"selected":""}>${v}%</option>`).join("")}</select></label><label class="inline-check"><input type="checkbox" id="reducedMotion" ${p.reducedMotion?"checked":""}>Уменьшить анимацию</label><div class="actions">${expButton("savePreferences","Сохранить оформление","check")}</div></section>`;
  }
  function wardrobe() {
    return `<section class="panel wardrobe-panel"><div class="wardrobe-heading"><h3>Гардероб</h3>${expButton("saveSkin","Сохранить текущий скин","plus")}</div><p class="note">Сохрани несколько вариантов и выбирай перед следующим входом в игру.</p><div id="wardrobe" class="wardrobe-grid"><p class="note">Загружаем сохранённые скины…</p></div></section>`;
  }
  async function loadWardrobe() {
    const el = $("#wardrobe"); if (!el) return;
    try {
      const data = await query("experienceState"); if (!el.isConnected) return;
      el.innerHTML = data.wardrobe.length ? data.wardrobe.map(s=>`<article class="wardrobe-card">${media(s.preview)?`<img src="${media(s.preview)}" alt="" loading="lazy">`:ic("user")}<strong>${esc(s.name)}</strong><small>${s.model==="slim"?"Slim":"Classic"}</small><div class="wardrobe-actions">${expButton("useSkin","Надеть","check",`data-skin="${s.id}"`)}${expButton("deleteSkin","Удалить","trash",`data-skin="${s.id}" ${wardrobeDeletes.has(s.id) ? "disabled" : ""}`)}</div></article>`).join("") : '<p class="note">В гардеробе пока пусто. Сначала выбери и сохрани скин в профиле.</p>';
    } catch(e) { if(el.isConnected) el.textContent = e.message; }
  }
  function galleryState(pid) {
    if (!galleries.has(pid)) galleries.set(pid, {images: [], trash: [], selected: new Map(), busy: false});
    return galleries.get(pid);
  }
  function gallery() {
    if (!active()) return emptyBuildView("Скриншоты", "Выбери сборку, чтобы открыть её снимки.");
    return head("Скриншоты", homeBuildPicker(), "Снимки Minecraft из папки screenshots выбранной сборки.") + `<div id="galleryControls" class="gallery-controls" aria-label="Управление скриншотами"></div><div id="screenshots" class="gallery-grid"><div class="empty">Читаем снимки…</div></div>`;
  }
  function updateGallerySelection(pid) {
    const data = galleryState(pid);
    const all = document.querySelector("[data-gallery-all]");
    if (all) {
      all.checked = data.images.length > 0 && data.selected.size === data.images.length;
      all.indeterminate = data.selected.size > 0 && data.selected.size < data.images.length;
      all.disabled = data.busy || !data.images.length;
    }
    for (const checkbox of document.querySelectorAll("[data-gallery-select]")) {
      checkbox.checked = data.selected.get(checkbox.dataset.gallerySelect) === checkbox.dataset.galleryToken;
      checkbox.disabled = data.busy;
      checkbox.closest(".gallery-card")?.classList.toggle("selected", checkbox.checked);
    }
    const count = $("#gallerySelectionCount"), remove = document.querySelector('[data-experience="deleteSelectedScreenshots"]');
    if (count) count.textContent = "Выбрано: " + data.selected.size;
    if (remove) remove.disabled = data.busy || !data.selected.size;
    const trash = document.querySelector('[data-experience="screenshotTrash"]');
    if (trash) trash.disabled = data.busy || !data.trash.length;
  }
  function setGalleryBusy(pid, busy) {
    galleryState(pid).busy = busy;
    if (view === "gallery" && active()?.id === pid) updateGallerySelection(pid);
    for (const button of document.querySelectorAll("[data-gallery-profile]")) {
      if (button.dataset.galleryProfile !== pid || !["deleteScreenshot", "restoreScreenshots", "purgeScreenshots"].includes(button.dataset.experience)) continue;
      button.disabled = busy;
      if (busy) button.setAttribute("aria-busy", "true");
      else button.removeAttribute("aria-busy");
    }
  }
  async function loadGallery() {
    const el = $("#screenshots"), p = active(); if (!el || !p) return;
    const request = ++galleryRequest;
    try {
      const result = await query("experienceGallery", {id:p.id}); if (!el.isConnected || request !== galleryRequest || active()?.id !== p.id) return;
      const data = galleryState(p.id);
      data.images = result.images; data.trash = result.trash || [];
      for (const [name, token] of data.selected) if (!data.images.some(x=>x.name===name && x.token===token)) data.selected.delete(name);
      $("#galleryControls").innerHTML = `<label class="inline-check"><input type="checkbox" data-gallery-all ${!data.images.length?"disabled":""}>Выбрать все показанные</label><span id="gallerySelectionCount" aria-live="polite"></span><div class="actions">${expButton("deleteSelectedScreenshots","Удалить выбранные","trash",'disabled')}${expButton("screenshotTrash", "Удалённые" + (data.trash.length ? " · " + data.trash.reduce((n,x)=>n+x.count,0) : ""), "folder", !data.trash.length?"disabled":"")}</div>`;
      el.innerHTML = data.images.length ? data.images.map(x=>`<figure class="gallery-card"><button class="gallery-open" data-experience="image" data-image="${x.image}" data-title="${esc(x.name)}" data-gallery-name="${esc(x.name)}" data-gallery-token="${x.token}" data-gallery-profile="${p.id}" aria-label="Открыть ${esc(x.name)}"><img src="${media(x.image)}" alt="${esc(x.name)}" loading="lazy"></button><figcaption><span>${esc(x.name)}</span><small>${date(x.modified)} · ${fmt(x.size)}</small><div class="gallery-card-actions"><label class="inline-check"><input type="checkbox" data-gallery-select="${esc(x.name)}" data-gallery-token="${x.token}" aria-label="Выбрать ${esc(x.name)}">Выбрать</label><button class="btn gallery-delete" data-experience="deleteScreenshot" data-gallery-name="${esc(x.name)}" data-gallery-token="${x.token}" data-gallery-profile="${p.id}" aria-label="Удалить ${esc(x.name)}">${ic("trash")}</button></div></figcaption></figure>`).join("") : '<div class="empty">Снимков пока нет. В Minecraft нажми F2.</div>';
      if (result.total > result.limit) el.insertAdjacentHTML("beforeend", `<p class="note gallery-limit">Показаны последние ${result.limit} снимков. Выбор относится только к ним; после удаления появятся следующие.</p>`);
      setGalleryBusy(p.id, data.busy);
    } catch(e) { if(el.isConnected && request === galleryRequest) el.textContent = e.message; }
  }
  async function changeScreenshots(pid, action, payload, button, {images = [], reopenTrash = false} = {}) {
    const data = galleryState(pid);
    if (data.busy) return;
    setGalleryBusy(pid, true);
    let result, error;
    try {
      result = await act(action, {id: pid, ...payload});
      for (const image of images) data.selected.delete(image.name);
    } catch (caught) { error = caught; }
    // The user may have closed this viewer or opened another dialog while the
    // request was in flight. Only the initiating, still-open dialog is ours.
    const currentDialog = button?.isConnected && button.closest("#dialog") && $("#dialog").open;
    if (currentDialog) closeDialog();
    try {
      if (view === "gallery" && active()?.id === pid) await loadGallery();
    } finally { setGalleryBusy(pid, false); }
    if (reopenTrash && currentDialog && button.isConnected && view === "gallery" && active()?.id === pid && !$("#dialog").open) screenshotTrash(pid);
    notice(error ? error.message : result.message, Boolean(error || result?.skipped?.length));
  }
  function deleteScreenshots(pid, images, button) {
    if (!images.length) return notice("Сначала выбери скриншоты.", true);
    return changeScreenshots(pid, "experienceDeleteScreenshots", {images}, button, {images});
  }
  function screenshotTrash(pid) {
    const data = galleryState(pid), profile = state.profiles.find(p=>p.id===pid);
    modal("Удалённые скриншоты · " + (profile?.name || "Сборка"), `<p class="note">Восстановление вернёт снимки в галерею. Существующие файлы с такими же именами сохраняются. Удаление из этого списка нельзя отменить.</p><div class="gallery-trash" tabindex="0" aria-label="Удалённые скриншоты">${data.trash.length ? data.trash.map(batch=>`<section class="gallery-trash-batch"><div><strong>${date(batch.created)}</strong><small>${batch.count} файлов · ${fmt(batch.size)}</small><details><summary>Имена файлов</summary><ul class="gallery-file-list">${batch.names.map(name=>`<li>${esc(name)}</li>`).join("")}</ul></details></div><div class="gallery-trash-actions">${expButton("restoreScreenshots", "Восстановить", "refresh", `data-gallery-profile="${pid}" data-gallery-batch="${batch.id}" ${data.busy ? "disabled" : ""}`)}${expButton("purgeScreenshots", "Удалить", "trash", `data-gallery-profile="${pid}" data-gallery-batch="${batch.id}" data-gallery-token="${esc(batch.token || "")}" ${data.busy ? "disabled" : ""}`)}</div></section>`).join("") : '<p class="note">Удалённых снимков нет.</p>'}</div>`);
  }
  function purgeScreenshots(pid, batchId, token, button) {
    const batch = galleryState(pid).trash.find(item => item.id === batchId);
    if (!token || token !== batch?.token) return notice("Обнови галерею и выбери удалённые скриншоты заново.", true);
    return changeScreenshots(pid, "experiencePurgeScreenshots", {batch: batchId, token}, button, {reopenTrash: true});
  }
  async function loadBuildErrors() {
    const list = $("#buildErrorsPanel [data-error-list]");
    const profile = active();
    if (!list || !profile) return;
    try {
      const data = await query("buildErrors", {id:profile.id});
      if (!list.isConnected) return;
      const groups = data.groups.map(group => {
        const last = group.events[0];
        const rows = group.events.map(event =>
          `<li><strong>${esc(event.stage)}</strong><small>${esc(date(event.created))}</small><pre>${esc(event.evidence)}</pre></li>`
        ).join("");
        return `<article class="diagnostic-finding"><div class="line"><h3>${esc(group.title)}</h3><small>${esc(date(last.created))}</small></div><p>${esc(group.advice)}</p><details><summary>Подробности (${group.events.length})</summary><ul class="build-error-events">${rows}</ul></details></article>`;
      }).join("");
      list.innerHTML = groups || '<p class="empty">Пока нет записанных ошибок.</p>';
    } catch (error) {
      if (list.isConnected) list.textContent = error.message;
    }
  }
  function showDiagnosticResults(p, data) {
    modal("Диагностика · " + p.name, `<p class="note">${esc(data.notice)}</p>${data.findings.length ? data.findings.map(f=>`<section class="diagnostic-finding"><div class="line"><h3>${esc(f.title)}</h3><span class="pill">${f.confidence==="high"?"Конкретный признак":"Возможная причина"}</span></div><p>${esc(f.advice)}</p><small>${esc(f.source)}</small><pre>${esc(f.evidence.join("\n"))}</pre></section>`).join("") : `<div class="empty">${data.empty?"Журнал ещё не создан. Сначала запусти сборку.":"Известных признаков ошибки не найдено. Это не гарантирует совместимость всех модов."}</div>`}<div class="actions diagnostic-actions" role="group" aria-label="Действия проверки сборки">${expButton("report","Посмотреть отчёт","folder")}<button class="btn" data-experience="manageDiagnosticBuild" data-experience-profile="${p.id}">${ic("layers")}Управлять содержимым</button></div>`);
  }
  async function diagnosticsBox(pid) {
    const p = pid ? state.profiles.find(x => x.id === pid) : active();
    if (!p) return notice("Сборка для диагностики не найдена.", true);
    modal("Диагностика · " + p.name, '<p id="diagnosticLoading">Читаем журнал и метаданные установленных модов…</p>');
    const el = $("#diagnosticLoading");
    const data = await query("diagnostics", {id: p.id});
    if (!el.isConnected) return;
    report = data.report;
    diagnosticSession = {profileId: p.id, data};
    showDiagnosticResults(p, data);
  }
  async function handle(action, el) {
    const p = active();
    if (action === "showBuildErrors") {
      const pid = el.dataset.experienceProfile;
      if (!state.profiles.some(item => item.id === pid)) return notice("Сборка для этой записи не найдена.", true);
      await act("select", {id:pid});
      kind = "errors";
      return navigateFresh("addons");
    }
    if (action === "diagnostics") return diagnosticsBox(el.dataset.experienceProfile);
    if (action === "manageDiagnosticBuild") {await act("select", {id:el.dataset.experienceProfile}); closeDialog(); view="addons"; kind="mods"; return refresh();}
    if (action === "savePreferences") { await act("experiencePreferences", {scale:Number($("#uiScale").value),reducedMotion:$("#reducedMotion").checked}); await refresh(); return notice("Оформление сохранено."); }
    if (action === "image") return modal(el.dataset.title, `<img class="gallery-full" src="${media(el.dataset.image)}" alt="${esc(el.dataset.title)}">`, {
      headerActions: expButton("deleteScreenshot", "Удалить", "trash", `data-gallery-name="${esc(el.dataset.galleryName)}" data-gallery-token="${el.dataset.galleryToken}" data-gallery-profile="${el.dataset.galleryProfile}" ${galleryState(el.dataset.galleryProfile).busy ? "disabled" : ""}`),
    });
    if (action === "deleteScreenshot") return deleteScreenshots(el.dataset.galleryProfile, [{name:el.dataset.galleryName, token:el.dataset.galleryToken}], el);
    if (action === "deleteSelectedScreenshots") return deleteScreenshots(p.id, [...galleryState(p.id).selected].map(([name,token])=>({name,token})), el);
    if (action === "screenshotTrash") return screenshotTrash(p.id);
    if (action === "purgeScreenshots") return purgeScreenshots(el.dataset.galleryProfile, el.dataset.galleryBatch, el.dataset.galleryToken, el);
    if (action === "restoreScreenshots") {
      return changeScreenshots(el.dataset.galleryProfile, "experienceRestoreScreenshots", {batch: el.dataset.galleryBatch}, el);
    }
    if (action === "notifications") return notifications();
    if (action === "clearNotifications") {
      el.disabled = true;
      try {
        await act("experienceClearNotifications");
        if (el.isConnected && $("#dialog").open) await notifications();
        return;
      } finally {
        if (el.isConnected) el.disabled = false;
      }
    }
    if (action === "saveSkin") {
      if (profileDraft?.file || (profileDraft && profileDraft.model !== state.model)) return notice("Сначала сохрани параметры профиля, чтобы добавить выбранный скин в гардероб.", true);
      if (!state.skin) return notice("Сначала выбери и сохрани скин игрока.", true);
      modal("Сохранить скин в гардероб", '<form id="wardrobeSave" class="stack"><label>Название<input class="field" id="wardrobeName" maxlength="60" value="Мой скин" required></label><button class="btn main" type="submit">Сохранить</button></form>');
      $("#wardrobeSave").onsubmit = async e=>{e.preventDefault();try{await act("experienceSaveSkin",{name:$("#wardrobeName").value});closeDialog();await loadWardrobe();notice("Скин добавлен в гардероб.");}catch(e){notice(e.message,true);}}; return;
    }
    if (action === "useSkin") {
      if (profileJob || profileSubmission) return notice("Дождись сохранения параметров профиля.", true);
      const draft = characterDraft(), file = draft.file, revision = draft.revisions.model;
      await act("experienceUseSkin", {skin:el.dataset.skin});
      state = await api("/api/state");
      // Choosing a saved skin must not discard an edited player name.
      if (draft.file === file && draft.revisions.model === revision) {
        draft.file = null;
        draft.model = state.model || "classic";
        draft.dirty.model = false;
      }
      render();
      return notice("Скин выбран. Он будет применён при следующем запуске игры.");
    }
    if (action === "deleteSkin") {
      const id = el.dataset.skin;
      if (wardrobeDeletes.has(id)) return;
      wardrobeDeletes.add(id);
      el.disabled = true;
      el.setAttribute("aria-busy", "true");
      try {
        await act("experienceDeleteSkin", {skin: id});
        await loadWardrobe();
        notice("Скин убран из гардероба.");
      } finally {
        wardrobeDeletes.delete(id);
        for (const button of document.querySelectorAll('[data-experience="deleteSkin"]')) {
          if (button.dataset.skin !== id) continue;
          button.disabled = false;
          button.removeAttribute("aria-busy");
        }
      }
      return;
    }
    if (action === "report") return modal("Отчёт для проверки", `<div class="diagnostic-report"><p>Проверь текст перед передачей другому человеку. Токены, типичные адреса и пути пользователя скрыты автоматически; мод может записывать сведения в произвольном формате.</p><textarea class="field report-text" id="diagnosticReport" spellcheck="false">${esc(report)}</textarea><div class="actions diagnostic-actions diagnostic-report-actions" role="group" aria-label="Действия с отчётом">${expButton("backToDiagnostics","Вернуться обратно","back")}${expButton("saveReport","Сохранить отчёт","download")}</div></div>`, {size: "large"});
    if (action === "backToDiagnostics") {
      const previous = diagnosticSession;
      const profile = state.profiles.find(item => item.id === previous?.profileId);
      if (!profile) return notice("Результат предыдущей проверки недоступен.", true);
      report = $("#diagnosticReport")?.value ?? report;
      return showDiagnosticResults(profile, previous.data);
    }
    if (action === "saveReport") return download($("#diagnosticReport").value, "BlockHarbor-diagnostics.txt");
  }
  document.addEventListener("click",e=>{const el=e.target.closest("[data-experience]");if(!el||el.disabled)return;e.preventDefault();handle(el.dataset.experience,el).catch(e=>notice(e.message,true));});
  document.addEventListener("input",e=>{if(e.target.id==="buildSearch"){buildQuery=e.target.value;filterRows();}});
  document.addEventListener("change",e=>{if(e.target.id==="onlyFavorites"){onlyFavorites=e.target.checked;filterRows();}});
  document.addEventListener("change", e=>{
    const input=e.target, pid=active()?.id;
    if (!pid || view!=="gallery") return;
    const data=galleryState(pid);
    if (input.matches("[data-gallery-all]")) {
      data.selected.clear();
      if (input.checked) for (const image of data.images) data.selected.set(image.name, image.token);
      updateGallerySelection(pid);
    } else if (input.matches("[data-gallery-select]")) {
      if (input.checked) data.selected.set(input.dataset.gallerySelect, input.dataset.galleryToken);
      else data.selected.delete(input.dataset.gallerySelect);
      updateGallerySelection(pid);
    }
  });
  // The existing picker performs format/compatibility checks for dropped files too.
  document.addEventListener("dragover",e=>{if(view==="addons"&&Object.hasOwn(kinds,kind)&&e.dataTransfer?.types.includes("Files")){e.preventDefault();if($("#filePick"))$("#view").classList.add("drop-ready");}});
  document.addEventListener("dragleave",e=>{if(!e.relatedTarget)$("#view").classList.remove("drop-ready");});
  document.addEventListener("drop",e=>{if(view!=="addons"||!e.dataTransfer?.files.length)return;e.preventDefault();if(!Object.hasOwn(kinds,kind))return notice("Открой вкладку модов, шейдеров или ресурс-паков.",true);$("#view").classList.remove("drop-ready");const input=$("#filePick");if(!input)return notice("Открой вкладку модов, шейдеров или ресурс-паков для добавления файлов.",true);input.files=e.dataTransfer.files;input.dispatchEvent(new Event("change"));});
  window.ExperienceUI = {settingsPanel,wardrobe,gallery,filterBar,preferences,media,observeNotifications,sortedProfiles:ps=>[...ps].sort((a,b)=>(b.favorite||0)-(a.favorite||0)),mount(){preferences();observeNotifications(state.notificationUnread ?? state.experience?.notificationUnread ?? 0);filterRows();if($("#buildErrorsPanel"))loadBuildErrors();if(view==="gallery")loadGallery();if(view==="profile")loadWardrobe();},dispatch:async action=>{if(action==="diagnostics"){await diagnosticsBox();return true;}return false;}};
})();
