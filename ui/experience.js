/* Local personalization, diagnostics and performance screens. */
(() => {
  let buildQuery = "", buildGroup = "", onlyFavorites = false;
  let report = "", performancePlan = null;
  const query = (action, p = {}) => api("/api/feature?" + new URLSearchParams({action, ...p}));
  const date = (v) => v ? new Date(v * 1000).toLocaleString("ru-RU", {dateStyle:"short",timeStyle:"short"}) : "—";
  const media = (digest) => /^[a-f0-9]{64}$/.test(digest || "") ? "/api/media/" + digest + ".png" : "";
  const expButton = (action, text, icon = "settings", extra = "") => `<button type="button" class="btn" data-experience="${action}" ${extra}>${ic(icon)}${text}</button>`;
  const download = (text, name, type = "text/plain;charset=utf-8") => {
    const url = URL.createObjectURL(new Blob([text], {type}));
    const a = document.createElement("a"); a.href = url; a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  };
  function preferences() {
    const p = state.experience?.preferences || {};
    document.documentElement.style.fontSize = (15 * (p.scale || 100) / 100) + "px";
    document.body.classList.toggle("reduced-motion", Boolean(p.reducedMotion));
  }
  function filterBar() {
    const groups = [...new Set(state.profiles.map(p => p.group_name).filter(Boolean))].sort();
    return `<div class="build-filter"><label class="search-field">${ic("search")}<input class="field" id="buildSearch" placeholder="Найти сборку…" value="${esc(buildQuery)}" aria-label="Поиск сборок"></label><select id="buildGroup" class="field" aria-label="Группа сборок"><option value="">Все группы</option>${groups.map(g=>`<option ${g===buildGroup?"selected":""}>${esc(g)}</option>`).join("")}</select><label class="inline-check"><input id="onlyFavorites" type="checkbox" ${onlyFavorites?"checked":""}>Избранное</label></div><p id="buildFilterEmpty" class="note" hidden>По этим условиям сборки не найдены.</p>`;
  }
  function filterRows() {
    let count = 0;
    for (const row of document.querySelectorAll(".build-row[data-profile]")) {
      const p = state.profiles.find(p => p.id === row.dataset.profile);
      if (!p) continue;
      const matches = (!buildQuery || [p.name,p.version,p.loader,p.description,p.group_name].join(" ").toLowerCase().includes(buildQuery.toLowerCase())) && (!buildGroup || p.group_name === buildGroup) && (!onlyFavorites || p.favorite);
      row.hidden = !matches; if (matches) count++;
    }
    if ($("#buildFilterEmpty")) $("#buildFilterEmpty").hidden = count > 0 || !state.profiles.length;
  }
  function toolbar(p) {
    return `<div class="experience-toolbar">${expButton("editBuild","Оформление","image")}${expButton("performance","Производительность","monitor")}${expButton("diagnostics","Диагностика","search")}${expButton("shortcut","Ярлык","link")}</div>`;
  }
  function settingsPanel() {
    const p = state.experience?.preferences || {};
    return `<section class="panel stack"><h3>Оформление и запуск</h3><label>Масштаб текста<select class="field" id="uiScale">${[85,90,100,110,120].map(v=>`<option value="${v}" ${(p.scale||100)===v?"selected":""}>${v}%</option>`).join("")}</select></label><label class="inline-check"><input type="checkbox" id="reducedMotion" ${p.reducedMotion?"checked":""}>Уменьшить анимацию</label><label class="inline-check"><input type="checkbox" id="checkUpdatesOnStart" ${p.checkUpdatesOnStart!==false?"checked":""}>Проверять обновления при запуске</label><div>${expButton("savePreferences","Сохранить оформление","check")}</div></section><section class="panel"><h3>Обновления и загрузки</h3><p>Проверка новых версий BlockHarbor, журнал загрузок и установка после завершения игры.</p><button class="btn" data-view="downloads">${ic("download")}Открыть центр загрузок</button></section>`;
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
  function gallery() {
    if (!active()) return emptyBuildView("Скриншоты", "Выбери сборку, чтобы открыть её снимки.");
    return head("Скриншоты", homeBuildPicker(), "Снимки Minecraft из папки screenshots выбранной сборки.") + `<div id="screenshots" class="gallery-grid"><div class="empty">Читаем снимки…</div></div>`;
  }
  async function loadGallery() {
    const el = $("#screenshots"), p = active(); if (!el || !p) return;
    try {
      const data = await query("experienceGallery", {id:p.id}); if (!el.isConnected) return;
      el.innerHTML = data.images.length ? data.images.map(x=>`<figure class="gallery-card"><button data-experience="image" data-image="${x.image}" data-title="${esc(x.name)}" aria-label="Открыть ${esc(x.name)}"><img src="${media(x.image)}" alt="${esc(x.name)}" loading="lazy"></button><figcaption>${esc(x.name)}<small>${date(x.modified)} · ${fmt(x.size)}</small></figcaption></figure>`).join("") : '<div class="empty">Снимков пока нет. В Minecraft нажми F2.</div>';
      if (data.total > data.limit) el.insertAdjacentHTML("beforeend", `<p class="note">Показаны последние ${data.limit} снимков. Все файлы остаются в папке сборки.</p>`);
    } catch(e) { if(el.isConnected) el.textContent = e.message; }
  }
  async function editBuild() {
    const p = active(); if (!p) return;
    modal("Оформление сборки", `<form id="buildAppearance" class="stack"><label>Название<input class="field" name="name" maxlength="48" required value="${esc(p.name)}"></label><label>Описание<textarea class="field" name="description" maxlength="1200" rows="3">${esc(p.description||"")}</textarea></label><label>Группа<input class="field" name="group" maxlength="40" placeholder="Например, С друзьями" value="${esc(p.group_name||"")}"></label><label>Цвет<select class="field" name="color">${[["green","Зелёный"],["blue","Синий"],["purple","Фиолетовый"],["orange","Оранжевый"],["rose","Розовый"]].map(([k,v])=>`<option value="${k}" ${p.color===k?"selected":""}>${v}</option>`).join("")}</select></label><label class="inline-check"><input name="favorite" type="checkbox" ${p.favorite?"checked":""}>В избранном</label><label>Обложка<input class="field" id="coverPick" type="file" accept="image/png,image/jpeg,image/webp"></label>${p.cover?'<label class="inline-check"><input type="checkbox" name="clearCover">Убрать текущую обложку</label>':""}<button type="submit" class="btn main">${ic("check")}Сохранить оформление</button></form>`);
    $("#buildAppearance").onsubmit = async e => {
      e.preventDefault(); const form = e.currentTarget, button = form.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        const f = new FormData(form), cover = $("#coverPick").files[0];
        if (cover) await api("/api/cover?id=" + p.id, {method:"POST",body:cover,headers:{"Content-Type":"application/octet-stream"}});
        await act("experienceSaveBuild", {id:p.id,name:f.get("name"),description:f.get("description"),group:f.get("group"),favorite:f.has("favorite"),color:f.get("color"),clearCover:!cover && f.has("clearCover")});
        closeDialog(); await refresh(); notice("Оформление сборки сохранено.");
      } catch(e) { notice(e.message,true); button.disabled=false; }
    };
  }
  async function diagnosticsBox() {
    const p = active(); if (!p) return;
    modal("Диагностика · " + p.name, '<p id="diagnosticLoading">Читаем журнал и метаданные установленных модов…</p>');
    const el = $("#diagnosticLoading");
    const data = await query("diagnostics", {id:p.id}); if (!el.isConnected) return;
    report = data.report;
    $("#dialogBody").innerHTML = `<p class="note">${esc(data.notice)}</p>${data.findings.length ? data.findings.map(f=>`<section class="diagnostic-finding"><div class="line"><h3>${esc(f.title)}</h3><span class="pill">${f.confidence==="high"?"Конкретный признак":"Возможная причина"}</span></div><p>${esc(f.advice)}</p><small>${esc(f.source)}</small><pre>${esc(f.evidence.join("\n"))}</pre></section>`).join("") : `<div class="empty">${data.empty?"Журнал ещё не создан. Сначала запусти сборку.":"Известных признаков ошибки не найдено. Это не гарантирует совместимость всех модов."}</div>`}<div class="actions">${expButton("report","Посмотреть отчёт","folder")}<button class="btn" data-view="addons">${ic("layers")}Управлять содержимым</button></div>`;
  }
  async function performanceBox() {
    const p = active(); if (!p) return;
    const data = await query("performanceState", {id:p.id});
    const metrics = data.metrics, recent = metrics?.recent || [];
    modal("Производительность · " + p.name, `<p>Изменения применяются только после предварительного просмотра. Перед сохранением создаётся точка восстановления.</p>${data.supported?`<div class="preset-grid">${data.presets.map(x=>`<button class="preset-card ${data.selected===x.id?"selected":""}" data-experience="previewPerformance" data-preset="${x.id}"><strong>${esc(x.name)}</strong><span>${esc(x.description)}</span>${data.selected===x.id?'<small>Выбран</small>':""}</button>`).join("")}</div>`:'<p class="note">Пресеты графики доступны с Minecraft 1.18.</p>'}<div class="actions">${expButton("performanceMods","Подобрать моды оптимизации","plug")}</div><h3>Измерения запусков</h3><p class="note">${esc(metrics?.label||"")}</p>${metrics?.current?`<p>Игра работает ${metrics.current.seconds} с · память ${metrics.current.residentBytes?fmt(metrics.current.residentBytes):"недоступна"}</p>`:""}${recent.length?`<div class="table-scroll"><table class="data-table"><thead><tr><th>Запуск</th><th>Подготовка</th><th>В игре</th><th>Пик памяти</th><th>Завершение</th></tr></thead><tbody>${recent.slice(0,8).map(r=>`<tr><td>${date(r.started)}</td><td>${r.preparation} с</td><td>${Math.round(r.duration)} с</td><td>${r.peak_rss?fmt(r.peak_rss):"—"}</td><td>${r.exit_code===0?"Штатное":"Код "+r.exit_code}</td></tr>`).join("")}</tbody></table></div>`:'<p class="note">Здесь появятся данные после запуска Minecraft из этой версии лаунчера.</p>'}`);
  }
  async function handle(action, el) {
    const p = active();
    if (action === "editBuild") return editBuild();
    if (action === "diagnostics") return diagnosticsBox();
    if (action === "performance") return performanceBox();
    if (action === "shortcut") { const r=await act("experienceShortcut",{id:p.id}); return notice(r.message); }
    if (action === "savePreferences") { await act("experiencePreferences", {scale:Number($("#uiScale").value),reducedMotion:$("#reducedMotion").checked,checkUpdatesOnStart:$("#checkUpdatesOnStart").checked}); await refresh(); return notice("Оформление сохранено."); }
    if (action === "image") return modal(el.dataset.title, `<img class="gallery-full" src="${media(el.dataset.image)}" alt="${esc(el.dataset.title)}">`);
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
    if (action === "useSkin") { await act("experienceUseSkin",{skin:el.dataset.skin}); profileDraft=null; await refresh(); return notice("Скин выбран. Он будет применён при следующем запуске игры."); }
    if (action === "deleteSkin") {
      const id=el.dataset.skin;
      modal("Убрать скин из гардероба?", '<p>Текущий скин персонажа останется выбранным.</p><button class="btn danger" id="confirmWardrobeDelete">Убрать</button>');
      $("#confirmWardrobeDelete").onclick=async()=>{try{await act("experienceDeleteSkin",{skin:id});closeDialog();await loadWardrobe();notice("Скин убран из гардероба.");}catch(e){notice(e.message,true);}};return;
    }
    if (action === "report") return modal("Отчёт для проверки", `<p>Проверь текст перед передачей другому человеку. Токены, типичные адреса и пути пользователя скрыты автоматически; мод может записывать сведения в произвольном формате.</p><textarea class="field report-text" id="diagnosticReport" spellcheck="false">${esc(report)}</textarea>${expButton("saveReport","Сохранить проверенный отчёт","download")}`);
    if (action === "saveReport") return download($("#diagnosticReport").value, "BlockHarbor-diagnostics.txt");
    if (action === "previewPerformance") {
      performancePlan = await query("performancePreview", {id:p.id,preset:el.dataset.preset});
      const x=performancePlan, ram=v=>v==="auto"?"Автоматически":v+" МБ";
      return modal("Профиль «"+x.title+"»", `<p>Настройки сборки «${esc(p.name)}»:</p><table class="data-table"><thead><tr><th>Параметр</th><th>Сейчас</th><th>После</th></tr></thead><tbody>${x.changes.map(r=>`<tr><td>${esc(r.label)}</td><td>${esc(r.before)}</td><td>${esc(r.after)}</td></tr>`).join("")}<tr><td>Память Java</td><td>${ram(x.memory.before)}</td><td>${ram(x.memory.after)}</td></tr></tbody></table><p class="note">Minecraft должен быть закрыт. Все предыдущие настройки останутся в точке восстановления.</p>${expButton("applyPerformance","Применить","check")}`);
    }
    if (action === "applyPerformance") {const plan=performancePlan;closeDialog();return runJob("performanceApply",{id:plan.id,preset:plan.preset,token:plan.token});}
    if (action === "performanceMods") {
      modal("Моды оптимизации",'<p id="optimizationLoading">Проверяем доступные релизы для этой версии и загрузчика…</p>');
      const box=$("#optimizationLoading"),data=await query("performanceRecommendations",{id:p.id});if(!box.isConnected)return;
      $("#dialogBody").innerHTML=`<p>${esc(data.message)}</p>${data.items.map(x=>`<section class="diagnostic-finding"><h3>${esc(x.title)}</h3><p>${esc(x.description)}</p>${x.available?`<p>${esc(x.version.number)}</p><button class="btn" data-experience="installOptimization" data-project="${x.project}" data-version="${x.version.id}" ${x.version.installed?"disabled":""}>${x.version.installed?"Установлен":"Проверить установку"}</button>`:`<small>${esc(x.error||"Совместимого стабильного релиза нет.")}</small>`}</section>`).join("")}`;return;
    }
    if(action==="installOptimization") {const data={id:p.id,kind:"mods",project:el.dataset.project,version:el.dataset.version};const check=await api("/api/modrinth/check?"+new URLSearchParams(data));if(check.blocked)throw new Error((check.conflicts||[]).join(" "));return runJob("installModrinth",data);}
  }
  document.addEventListener("click",e=>{const el=e.target.closest("[data-experience]");if(!el)return;e.preventDefault();handle(el.dataset.experience,el).catch(e=>notice(e.message,true));});
  document.addEventListener("input",e=>{if(e.target.id==="buildSearch"){buildQuery=e.target.value;filterRows();}});
  document.addEventListener("change",e=>{if(e.target.id==="buildGroup"){buildGroup=e.target.value;filterRows();}if(e.target.id==="onlyFavorites"){onlyFavorites=e.target.checked;filterRows();}});
  // The existing picker performs format/compatibility checks for dropped files too.
  document.addEventListener("dragover",e=>{if(view==="addons"&&e.dataTransfer?.types.includes("Files")){e.preventDefault();$("#view").classList.add("drop-ready");}});
  document.addEventListener("dragleave",e=>{if(!e.relatedTarget)$("#view").classList.remove("drop-ready");});
  document.addEventListener("drop",e=>{if(view!=="addons"||!e.dataTransfer?.files.length)return;e.preventDefault();$("#view").classList.remove("drop-ready");const input=$("#filePick");input.files=e.dataTransfer.files;input.dispatchEvent(new Event("change"));});
  window.ExperienceUI = {toolbar,settingsPanel,wardrobe,gallery,filterBar,preferences,media,sortedProfiles:ps=>[...ps].sort((a,b)=>(b.favorite||0)-(a.favorite||0)),mount(){preferences();filterRows();if(view==="gallery")loadGallery();if(view==="profile")loadWardrobe();},dispatch:async action=>{if(action==="diagnostics"){await diagnosticsBox();return true;}return false;}};
})();
