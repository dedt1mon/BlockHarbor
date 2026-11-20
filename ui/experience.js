/* Local preferences, diagnostics, skins and screenshots. */
(() => {
  let buildQuery = "", onlyFavorites = false;
  let report = "";
  const galleries = new Map();
  let galleryRequest = 0;
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
    return `<section class="panel stack"><h3>Оформление</h3><label>Масштаб текста<select class="field" id="uiScale">${[85,90,100,110,120].map(v=>`<option value="${v}" ${(p.scale||100)===v?"selected":""}>${v}%</option>`).join("")}</select></label><label class="inline-check"><input type="checkbox" id="reducedMotion" ${p.reducedMotion?"checked":""}>Уменьшить анимацию</label><div>${expButton("savePreferences","Сохранить оформление","check")}</div></section>`;
  }
  function wardrobe() {
    return `<section class="panel"><div class="splitbar"><h3>Гардероб</h3>${expButton("saveSkin","Сохранить текущий скин","plus")}</div><p class="note">Сохрани несколько вариантов и выбирай перед следующим входом в игру.</p><div id="wardrobe" class="wardrobe-grid"><p class="note">Загружаем сохранённые скины…</p></div></section>`;
  }
  async function loadWardrobe() {
    const el = $("#wardrobe"); if (!el) return;
    try {
      const data = await query("experienceState"); if (!el.isConnected) return;
      el.innerHTML = data.wardrobe.length ? data.wardrobe.map(s=>`<article class="wardrobe-card">${media(s.preview)?`<img src="${media(s.preview)}" alt="" loading="lazy">`:ic("user")}<strong>${esc(s.name)}</strong><small>${s.model==="slim"?"Slim":"Classic"}</small><div class="actions">${expButton("useSkin","Надеть","check",`data-skin="${s.id}"`)}${expButton("deleteSkin","Удалить","trash",`data-skin="${s.id}"`)}</div></article>`).join("") : '<p class="note">В гардеробе пока пусто. Сначала выбери и сохрани скин в профиле.</p>';
    } catch(e) { if(el.isConnected) el.textContent = e.message; }
  }
  function galleryState(pid) {
    if (!galleries.has(pid)) galleries.set(pid, {images: [], trash: [], selected: new Map()});
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
    }
    for (const checkbox of document.querySelectorAll("[data-gallery-select]")) {
      checkbox.checked = data.selected.get(checkbox.dataset.gallerySelect) === checkbox.dataset.galleryToken;
      checkbox.closest(".gallery-card")?.classList.toggle("selected", checkbox.checked);
    }
    const count = $("#gallerySelectionCount"), remove = document.querySelector('[data-experience="deleteSelectedScreenshots"]');
    if (count) count.textContent = "Выбрано: " + data.selected.size;
    if (remove) remove.disabled = !data.selected.size;
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
      updateGallerySelection(p.id);
    } catch(e) { if(el.isConnected && request === galleryRequest) el.textContent = e.message; }
  }
  async function deleteScreenshots(pid, images) {
    if (!images.length) return notice("Сначала выбери скриншоты.", true);
    const profile = state.profiles.find(p=>p.id===pid);
    modal(images.length === 1 ? "Удалить скриншот?" : "Удалить выбранные скриншоты?", `<p>Сборка «${esc(profile?.name || "Выбранная сборка") }». Файлов: <strong>${images.length}</strong>.</p><ul class="gallery-file-list">${images.map(x=>`<li>${esc(x.name)}</li>`).join("")}</ul><p class="note">Снимки переместятся в удалённые. Их можно будет вернуть кнопкой «Удалённые» в галерее.</p><div class="actions"><button class="btn" data-experience="cancelScreenshotDelete">Отмена</button><button class="btn danger" id="confirmScreenshotDelete">${ic("trash")}Удалить · ${images.length}</button></div>`);
    $("#confirmScreenshotDelete").onclick = async event => {
      const button = event.currentTarget; button.disabled = true;
      try {
        const result = await act("experienceDeleteScreenshots", {id:pid, images});
        for (const x of images) galleryState(pid).selected.delete(x.name);
        if (button.isConnected) closeDialog();
        if (view === "gallery" && active()?.id === pid) await loadGallery();
        notice(result.message, Boolean(result.skipped?.length));
      } catch(e) {
        if (button.isConnected) closeDialog();
        if (view === "gallery" && active()?.id === pid) await loadGallery();
        notice(e.message, true);
      }
    };
  }
  function screenshotTrash(pid) {
    const data = galleryState(pid), profile = state.profiles.find(p=>p.id===pid);
    modal("Удалённые скриншоты · " + (profile?.name || "Сборка"), `<p class="note">Восстановление вернёт снимки в галерею. Существующие файлы с такими же именами сохраняются.</p><div class="gallery-trash">${data.trash.length ? data.trash.map(batch=>`<section class="gallery-trash-batch"><div><strong>${date(batch.created)}</strong><small>${batch.count} файлов · ${fmt(batch.size)}</small><details><summary>Имена файлов</summary><ul class="gallery-file-list">${batch.names.map(name=>`<li>${esc(name)}</li>`).join("")}</ul></details></div>${expButton("restoreScreenshots", "Восстановить", "refresh", `data-gallery-profile="${pid}" data-gallery-batch="${batch.id}"`)}</section>`).join("") : '<p class="note">Удалённых снимков нет.</p>'}</div>`);
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
      list.innerHTML = (groups || '<p class="empty">Пока нет записанных ошибок.</p>') +
        expButton("diagnostics", "Проверить сборку", "search", `data-experience-profile="${esc(profile.id)}"`);
    } catch (error) {
      if (list.isConnected) list.textContent = error.message;
    }
  }
  async function diagnosticsBox(pid) {
    const p = pid ? state.profiles.find(x=>x.id===pid) : active(); if (!p) return notice("Сборка для диагностики не найдена.", true);
    modal("Диагностика · " + p.name, '<p id="diagnosticLoading">Читаем журнал и метаданные установленных модов…</p>');
    const el = $("#diagnosticLoading");
    const data = await query("diagnostics", {id:p.id}); if (!el.isConnected) return;
    report = data.report;
    $("#dialogBody").innerHTML = `<p class="note">${esc(data.notice)}</p>${data.findings.length ? data.findings.map(f=>`<section class="diagnostic-finding"><div class="line"><h3>${esc(f.title)}</h3><span class="pill">${f.confidence==="high"?"Конкретный признак":"Возможная причина"}</span></div><p>${esc(f.advice)}</p><small>${esc(f.source)}</small><pre>${esc(f.evidence.join("\n"))}</pre></section>`).join("") : `<div class="empty">${data.empty?"Журнал ещё не создан. Сначала запусти сборку.":"Известных признаков ошибки не найдено. Это не гарантирует совместимость всех модов."}</div>`}<div class="actions">${expButton("report","Посмотреть отчёт","folder")}<button class="btn" data-experience="manageDiagnosticBuild" data-experience-profile="${p.id}">${ic("layers")}Управлять содержимым</button></div>`;
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
    if (action === "image") return modal(el.dataset.title, `<img class="gallery-full" src="${media(el.dataset.image)}" alt="${esc(el.dataset.title)}"><div class="gallery-viewer-actions">${expButton("deleteScreenshot", "Удалить скриншот", "trash", `data-gallery-name="${esc(el.dataset.galleryName)}" data-gallery-token="${el.dataset.galleryToken}" data-gallery-profile="${el.dataset.galleryProfile}"`)}</div>`);
    if (action === "deleteScreenshot") return deleteScreenshots(el.dataset.galleryProfile, [{name:el.dataset.galleryName, token:el.dataset.galleryToken}]);
    if (action === "deleteSelectedScreenshots") return deleteScreenshots(p.id, [...galleryState(p.id).selected].map(([name,token])=>({name,token})));
    if (action === "cancelScreenshotDelete") return closeDialog();
    if (action === "screenshotTrash") return screenshotTrash(p.id);
    if (action === "restoreScreenshots") {
      el.disabled = true;
      try {
        const result = await act("experienceRestoreScreenshots", {id:el.dataset.galleryProfile, batch:el.dataset.galleryBatch});
        if (el.isConnected) closeDialog();
        if (view === "gallery" && active()?.id === el.dataset.galleryProfile) await loadGallery();
        notice(result.message);
      } catch(e) {el.disabled=false; throw e;}
      return;
    }
    if (action === "notifications") {
      const data = await query("experienceNotifications");
      return modal("История уведомлений", `<div class="notification-history">${data.items.length?data.items.map(n=>`<article class="notification-item ${n.level}"><small>${date(n.created)}</small><p>${esc(n.message)}</p></article>`).join(""):'<p class="note">История пока пуста.</p>'}</div>${expButton("clearNotifications","Очистить историю","trash")}`);
    }
    if (action === "clearNotifications") { await act("experienceClearNotifications"); closeDialog(); return notice("История очищена."); }
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
      const id=el.dataset.skin;
      modal("Убрать скин из гардероба?", '<p>Текущий скин персонажа останется выбранным.</p><button class="btn danger" id="confirmWardrobeDelete">Убрать</button>');
      $("#confirmWardrobeDelete").onclick=async()=>{try{await act("experienceDeleteSkin",{skin:id});closeDialog();await loadWardrobe();notice("Скин убран из гардероба.");}catch(e){notice(e.message,true);}};return;
    }
    if (action === "report") return modal("Отчёт для проверки", `<p>Проверь текст перед передачей другому человеку. Токены, типичные адреса и пути пользователя скрыты автоматически; мод может записывать сведения в произвольном формате.</p><textarea class="field report-text" id="diagnosticReport" spellcheck="false">${esc(report)}</textarea>${expButton("saveReport","Сохранить проверенный отчёт","download")}`);
    if (action === "saveReport") return download($("#diagnosticReport").value, "BlockHarbor-diagnostics.txt");
  }
  document.addEventListener("click",e=>{const el=e.target.closest("[data-experience]");if(!el)return;e.preventDefault();handle(el.dataset.experience,el).catch(e=>notice(e.message,true));});
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
  window.ExperienceUI = {settingsPanel,wardrobe,gallery,filterBar,preferences,media,sortedProfiles:ps=>[...ps].sort((a,b)=>(b.favorite||0)-(a.favorite||0)),mount(){preferences();filterRows();if($("#buildErrorsPanel"))loadBuildErrors();if(view==="gallery")loadGallery();if(view==="profile")loadWardrobe();},dispatch:async action=>{if(action==="diagnostics"){await diagnosticsBox();return true;}return false;}};
})();
