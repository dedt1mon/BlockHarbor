"use strict";

// The catalog uses the existing modal host; filters, results and chosen releases
// stay independent of the build list and background refreshes.
(() => {
  const catalog = {q: "", version: "", loader: "", category: "", sort: "relevance", offset: 0, nextOffset: 0, page: null, error: "", busy: false, needsSearch: true};
  const choices = new Map();
  let searchGeneration = 0, selectedProject = null, prepared = null, shared = null, comparison = null;
  let searchController = null, releaseController = null, catalogScroll = 0, returnFocus = null, catalogOpen = false;
  const feature = (action, params = {}, options = {}) => api("/api/feature?" + new URLSearchParams({action, ...params}), options);
  const button = (attr, label, icon = "", cls = "") => `<button type="button" class="btn ${cls}" ${attr}>${icon ? ic(icon) : ""}${label}</button>`;
  const option = (value, label, selected) => `<option value="${esc(value)}"${selected === value ? " selected" : ""}>${esc(label)}</option>`;
  const empty = (message) => `<div class="empty">${esc(message)}</div>`;
  const number = new Intl.NumberFormat("ru-RU");

  function discardPrepared() {
    if (!prepared) return;
    const token = prepared.token;
    prepared = null;
    act("packDiscard", {token}).catch(() => {});
  }

  function stopSearch() {
    ++searchGeneration;
    searchController?.abort();
    searchController = null;
    catalog.busy = false;
  }

  function packModal(title, content, page) {
    if (!catalogOpen) {
      returnFocus = document.activeElement !== document.body ? document.activeElement : document.querySelector('[data-act="packCatalog"]');
      catalogOpen = true;
    }
    modal(title, `<div class="pack-dialog" data-pack-view="${page}">${content}</div>`);
  }

  function rememberCatalog() {
    const host = document.querySelector(".pack-hub");
    if (!host) return;
    catalog.q = host.querySelector('[name="q"]').value;
    catalogScroll = host.querySelector(".pack-results").scrollTop;
  }

  function openCatalog({focusProject = ""} = {}) {
    discardPrepared();
    releaseController?.abort();
    releaseController = null;
    packModal("Каталог сборок", renderPacks(), "catalog");
    loadArt();
    const host = document.querySelector(".pack-hub");
    host.querySelector(".pack-results").scrollTop = catalogScroll;
    const previous = [...host.querySelectorAll("[data-pack-project]")].find(button => button.dataset.packProject === focusProject);
    (previous || host.querySelector('[name="q"]'))?.focus({preventScroll: true});
    if ((!catalog.page || catalog.needsSearch) && !catalog.busy) search(catalog.nextOffset);
  }

  function results() {
    if (!catalog.page) return empty(catalog.error || "Выбери фильтры или найди готовую сборку по названию.");
    if (!catalog.page.hits.length) return empty("Модпаки не найдены. Попробуй другие фильтры.");
    return `<div class="pack-grid">${catalog.page.hits.map(item => `<article class="pack-card">
      <div class="pack-card-top"><span class="pack-art" data-pack-icon="${esc(item.icon)}">${ic("layers")}</span><div><h3>${esc(item.title)}</h3><span class="meta">${esc(item.author)}</span></div></div>
      <p>${esc(item.description)}</p><div class="pack-card-footer"><span class="meta">${number.format(item.downloads)} загрузок</span>${button(`data-pack-project="${esc(item.id)}"`, "Выбрать выпуск", "arrow")}</div>
    </article>`).join("")}</div>`;
  }

  function pager() {
    const page = catalog.page;
    if (!page) return "";
    return `<div class="pack-pager">${button('data-pack-page="previous"' + (catalog.offset <= 0 || catalog.busy ? " disabled" : ""), "Назад", "back")}<span class="meta">${page.total ? `${page.offset + 1}–${Math.min(page.total, page.offset + page.limit)} из ${number.format(page.total)}` : "0 результатов"}</span>${button('data-pack-page="next"' + (catalog.offset + page.limit >= page.total || catalog.busy ? " disabled" : ""), "Далее", "arrow")}</div>`;
  }

  function renderPacks() {
    return `<section class="pack-hub"><div class="pack-intro"><p>Готовые сборки с Modrinth устанавливаются отдельно, со своей версией Minecraft и настройками.</p>${button("data-pack-import", "Импорт .mrpack", "upload")}</div><form class="pack-search" data-pack-search role="search"><input type="search" class="field" name="q" maxlength="180" placeholder="Найти модпак" aria-label="Название модпака" value="${esc(catalog.q)}"><button class="btn main" type="submit">${ic("search")}Найти</button></form>
      <div class="pack-filters"><label>Minecraft<select class="field" data-pack-filter="version">${option("", "Все версии", catalog.version)}${(state.versions || []).map(v => option(v, v, catalog.version)).join("")}</select></label>
      <label>Загрузчик<select class="field" data-pack-filter="loader">${option("", "Любой", catalog.loader)}${Object.entries(loaders).map(([v, label]) => option(v, label, catalog.loader)).join("")}</select></label>
      <label>Категория<select class="field" data-pack-filter="category">${Object.entries({"": "Все категории", adventure: "Приключения", challenging: "Испытания", combat: "Сражения", "kitchen-sink": "Всё вместе", lightweight: "Лёгкие сборки", magic: "Магия", multiplayer: "Для друзей", optimization: "Оптимизация", quests: "Квесты", technology: "Технологии"}).map(([v, label]) => option(v, label, catalog.category)).join("")}</select></label>
      <label>Сортировка<select class="field" data-pack-filter="sort">${Object.entries({relevance: "По релевантности", downloads: "По загрузкам", follows: "По подписчикам", newest: "Сначала новые", updated: "Недавно обновлённые"}).map(([v, label]) => option(v, label, catalog.sort)).join("")}</select></label></div>
      <p class="pack-status${catalog.error ? " pack-error" : ""}" role="status">${esc(catalog.error || (catalog.busy ? "Загружаем каталог…" : ""))}</p><div class="pack-results" aria-busy="${catalog.busy}">${results()}</div><div class="pack-page-host">${pager()}</div></section>`;
  }

  function loadArt() {
    for (const slot of document.querySelectorAll("[data-pack-icon]")) {
      if (slot.querySelector("img")) continue;
      const url = slot.dataset.packIcon;
      if (!/^https:\/\/cdn\.modrinth\.com\//.test(url)) continue;
      const image = new Image(48, 48);
      image.alt = "";
      image.loading = "lazy";
      image.decoding = "async";
      image.addEventListener("error", () => image.remove(), {once: true});
      slot.append(image);
      image.src = url;
    }
  }

  function updateResults() {
    const host = document.querySelector(".pack-hub");
    if (!host) return;
    host.querySelector(".pack-results").innerHTML = results();
    host.querySelector(".pack-results").setAttribute("aria-busy", String(catalog.busy));
    host.querySelector(".pack-page-host").innerHTML = pager();
    const status = host.querySelector(".pack-status");
    status.textContent = catalog.error || (catalog.busy ? "Загружаем каталог…" : "");
    status.classList.toggle("pack-error", !!catalog.error);
    loadArt();
  }

  async function search(offset = 0) {
    const generation = ++searchGeneration;
    searchController?.abort();
    const controller = new AbortController();
    searchController = controller;
    catalog.nextOffset = offset;
    catalog.needsSearch = true;
    catalog.busy = true;
    catalog.error = "";
    // Keep the previous page visible until a replacement has arrived.
    const host = document.querySelector(".pack-hub");
    if (host) {
      host.querySelector(".pack-results").setAttribute("aria-busy", "true");
      host.querySelector(".pack-status").textContent = "Загружаем каталог…";
      host.querySelector(".pack-page-host").innerHTML = pager();
    }
    try {
      const page = await feature("packSearch", {q: catalog.q, version: catalog.version, loader: catalog.loader, category: catalog.category, sort: catalog.sort, offset}, {signal: controller.signal});
      if (generation !== searchGeneration || controller.signal.aborted) return;
      catalog.page = page;
      catalog.offset = offset;
      catalog.needsSearch = false;
      catalogScroll = 0;
    } catch (error) {
      if (generation === searchGeneration && !controller.signal.aborted) catalog.error = error.message;
    } finally {
      if (generation === searchGeneration) {
        catalog.busy = false;
        searchController = null;
        updateResults();
        document.querySelector(".pack-hub .pack-results")?.scrollTo?.(0, 0);
      }
    }
  }

  async function showReleases(project) {
    rememberCatalog();
    stopSearch();
    discardPrepared();
    releaseController?.abort();
    const controller = new AbortController();
    releaseController = controller;
    selectedProject = project;
    packModal("Выпуски модпака", `${button('data-pack-back="catalog"', "К каталогу", "back", "pack-back")}<div class="pack-release" role="status">Получаем версии…</div>`, "releases");
    const host = document.querySelector(".pack-release");
    try {
      const data = await feature("packVersions", {project}, {signal: controller.signal});
      if (!host.isConnected || controller.signal.aborted || selectedProject !== project) return;
      const selected = data.versions.find(v => v.id === choices.get(project))?.id || data.versions.find(v => v.type === "release")?.id || data.versions[0]?.id;
      if (!selected) {host.innerHTML = empty("У этого модпака нет доступных выпусков .mrpack."); return;}
      choices.set(project, selected);
      host.innerHTML = `<h3>${esc(data.title)}</h3><p>${esc(data.description)}</p><label>Выпуск<select class="field" data-pack-version="${esc(project)}">${data.versions.map(v => option(v.id, `${v.number || v.name} · ${v.minecraft.join(", ")} · ${v.loaders.join(", ")}${v.type !== "release" ? " · " + v.type : ""}`, selected)).join("")}</select></label><p class="note">Перед установкой покажем точную версию игры, размер загрузки и необязательные компоненты.</p><div class="dialog-actions">${button("data-pack-prepare", "Подготовить установку", "download", "main")}</div>`;
      host.querySelector("select")?.focus({preventScroll: true});
    } catch (error) {
      if (host.isConnected && !controller.signal.aborted) host.textContent = error.message;
    } finally {
      if (releaseController === controller) releaseController = null;
    }
  }

  function previewPack(data, {project = null} = {}) {
    if (prepared && prepared.token !== data.token) discardPrepared();
    prepared = data;
    selectedProject = project;
    packModal("Установить модпак", `${button(`data-pack-back="${project ? "releases" : "catalog"}"`, project ? "К выпускам" : "К каталогу", "back", "pack-back")}<section class="pack-preview"><h3>${esc(data.title)}</h3><p>${esc(data.summary)}</p><div class="tags"><span class="pill">Minecraft ${esc(data.minecraft)}</span><span class="pill">${esc(loaders[data.loader] || data.loader)} ${esc(data.loaderVersion || "")}</span><span class="pill">${data.files} файлов</span><span class="pill">${fmt(data.downloadBytes)} загрузка</span></div>
      <label>Название новой сборки<input class="field" data-pack-name maxlength="48" value="${esc(data.name)}"></label>
      ${data.optional.length ? `<h4>Необязательные компоненты</h4><div class="pack-optional">${data.optional.map(row => `<label><input type="checkbox" data-pack-optional value="${esc(row.path)}"><span>${esc(row.path)}</span><small>${fmt(row.size)}</small></label>`).join("")}</div>` : ""}
      ${data.unsupported ? `<p class="note">${data.unsupported} файлов только для выделенного сервера будут пропущены.</p>` : ""}
      <p class="note">Текущие сборки останутся на месте. Файлы проверяются по размеру, SHA-1 и SHA-512.</p><div class="dialog-actions">${button("data-pack-install", "Установить отдельную сборку", "download", "main")}</div></section>`, "preview");
  }

  async function importPack() {
    let picker = document.querySelector("#mrpackPick");
    if (!picker) {
      picker = document.createElement("input");
      picker.type = "file";
      picker.id = "mrpackPick";
      picker.accept = ".mrpack,application/x-modrinth-modpack+zip";
      picker.hidden = true;
      document.body.append(picker);
      picker.addEventListener("change", async () => {
        const file = picker.files[0];
        picker.value = "";
        if (!file) return;
        if (!file.size || file.size > 2 * 1024 ** 3) return notice("Выбери .mrpack размером до 2 ГБ.", true);
        picker.disabled = true;
        rememberCatalog();
        stopSearch();
        releaseController?.abort();
        discardPrepared();
        packModal("Проверка модпака", `${button('data-pack-back="catalog"', "К каталогу", "back", "pack-back")}<div class="pack-import-pending" role="status">Читаем файл и проверяем состав…</div>`, "import");
        const pendingHost = document.querySelector(".pack-import-pending");
        try {
          const data = await api("/api/mrpack-import", {method: "POST", headers: {"Content-Type": "application/x-modrinth-modpack+zip"}, body: file});
          if (!pendingHost.isConnected || !document.querySelector("#dialog").open) {
            await act("packDiscard", {token: data.token});
            return;
          }
          previewPack(data);
        } catch (error) {if (pendingHost.isConnected && document.querySelector("#dialog").open) pendingHost.textContent = error.message;}
        finally {picker.disabled = false;}
      });
    }
    picker.click();
  }

  async function exportPack(id) {
    notice("Готовим .mrpack…", false, true);
    const data = await api("/api/mrpack-export", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id})});
    const link = document.createElement("a");
    link.href = data.download;
    link.download = data.name;
    link.click();
    notice("Модпак готов к скачиванию.");
  }

  function friendsPanel(profile) {
    if (!profile) return `<section class="panel share-panel"><div class="line">${ic("layers")}<h3>Сборка друга</h3></div><p>Открой описание .bhshare или вставь код приглашения. BlockHarbor покажет состав и подготовит новую сборку.</p>${button("data-share-import", "Подготовить сборку друга", "download", "main")}</section>`;
    return `<section class="panel share-panel"><div class="line">${ic("layers")}<h3>Одинаковая сборка для друзей</h3></div><p>Обменяйтесь описанием сборки. BlockHarbor покажет недостающие моды, разные версии и личные клиентские дополнения.</p><div class="actions">${button("data-share-create", "Поделиться составом", "upload")}${button("data-share-import", "Сравнить со сборкой друга", "search", "main")}${button("data-share-invite", "Приглашение в мир", "users")}</div><p class="note">Код содержит состав сборки и, если добавлен, адрес текущего мира. Передай его другу любым удобным способом.</p><details class="share-status-details"><summary>Последний сигнал e4steam</summary><p class="share-room-state">Нажми «Обновить», чтобы прочитать текущий журнал.</p>${button("data-share-status", "Обновить", "refresh")}</details></section>`;
  }

  function showShareResult(data) {
    shared = data;
    modal(data.descriptor.invite ? "Приглашение в мир" : "Поделиться составом сборки", `<section class="share-result"><h3>${esc(data.descriptor.name)}</h3><p>Minecraft ${esc(data.descriptor.minecraft)} · ${esc(loaders[data.descriptor.loader])} · ${data.descriptor.files.length} дополнений</p>${data.descriptor.invite ? `<p class="share-address">${esc(data.descriptor.invite.address)}</p><p class="note">Приглашение действует только пока хозяин держит этот сеанс открытым. Право входа проверяет Steam.</p>` : ""}<label>Код для друга<textarea class="field share-code" readonly spellcheck="false">${esc(data.code)}</textarea></label><div class="actions">${button("data-share-copy", "Скопировать код", "copy", "main")}${button("data-share-save", "Скачать .bhshare", "download")}</div>${data.manual ? `<p class="note">Для ${data.manual} обязательных файлов нет источника загрузки. Друг увидит их в списке для ручного добавления; можно также передать обычный ZIP сборки.</p>` : ""}</section>`);
  }

  function compareBox() {
    modal("Сравнить сборки", `<section class="share-compare"><p>Сравним описание с «${esc(active()?.name || "Новая сборка")}». До подтверждения файлы останутся на месте.</p><label>Код или JSON описания<textarea class="field share-code" data-share-input placeholder="bhshare:…" spellcheck="false"></textarea></label><div class="actions">${button("data-share-load", "Открыть .bhshare", "folder")}${button("data-share-compare", "Сравнить", "search", "main")}</div></section>`);
  }

  function shareRows(rows, label) {
    if (!rows.length) return "";
    return `<h4>${label} · ${rows.length}</h4><ul class="share-files">${rows.map(row => `<li><strong>${esc(row.title || row.path)}</strong><span>${esc(row.number || row.path)}</span></li>`).join("")}</ul>`;
  }

  function comparisonView(data) {
    comparison = data;
    modal("Сравнение со сборкой «" + data.name + "»", `<section class="share-review"><div class="tags"><span class="pill">Minecraft ${esc(data.minecraft)}</span><span class="pill">${esc(loaders[data.loader])}</span><span class="pill">${data.matching} совпадают</span></div>
      ${shareRows(data.missing, "Нужно добавить")}${data.different.length ? `<h4>Разные версии · ${data.different.length}</h4><ul class="share-files">${data.different.map(row => `<li><strong>${esc(row.wanted.title)}</strong><span>${esc(row.current.number || row.current.path)} → ${esc(row.wanted.number || row.wanted.path)}</span></li>`).join("")}</ul>` : ""}
      ${shareRows(data.optional, "Личные компоненты друга — можно не устанавливать")}${shareRows(data.extra, "Твои дополнения — сохранятся")}
      ${data.warnings.map(text => `<p class="note">${esc(text)}</p>`).join("")}
      ${data.manual.length ? `<div class="share-warning"><strong>Нужны файлы от друга</strong><p>У этих дополнений нет проверяемого источника. Добавь их вручную или загрузи ZIP сборки, затем повтори сравнение.</p>${shareRows(data.manual, "Файлы")}</div>` : ""}
      ${data.ready ? '<p class="share-success">Обязательные файлы совпадают.</p>' : `<p>Будет загружено ${fmt(data.downloadBytes)}. Миры и сохранения исходной сборки останутся на месте.</p>`}
      <div class="dialog-actions">${data.canApply ? button("data-share-apply=\"clone\"", "Подготовить отдельную сборку", "copy", "main") : ""}${data.canApply && data.sameBase && !data.ready ? button("data-share-apply=\"existing\"", "Применить к текущей", "refresh") : ""}${data.invite && data.ready ? button("data-share-add-server", "Добавить мир в Minecraft", "users", "main") : ""}</div>
      ${data.invite ? `<p class="note">Адрес мира: <span class="share-address">${esc(data.invite.address)}</span>. После синхронизации повтори сравнение и добавь мир в список серверов.</p>` : ""}
      </section>`);
  }

  async function loadShareFile() {
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = ".bhshare,application/json";
    picker.addEventListener("change", async () => {
      const file = picker.files[0];
      if (!file) return;
      if (file.size > 4 * 1024 ** 2) return notice("Описание сборки превышает 4 МБ.", true);
      const input = document.querySelector("[data-share-input]");
      if (input) input.value = await file.text();
    }, {once: true});
    picker.click();
  }

  document.addEventListener("submit", event => {
    if (!event.target.matches("[data-pack-search]")) return;
    event.preventDefault();
    catalog.q = new FormData(event.target).get("q") || "";
    search(0);
  });
  document.addEventListener("change", event => {
    const filter = event.target.dataset.packFilter;
    if (filter) {
      catalog[filter] = event.target.value;
      catalog.q = document.querySelector("[data-pack-search] input")?.value || catalog.q;
      search(0);
    }
    const project = event.target.dataset.packVersion;
    if (project) choices.set(project, event.target.value);
  });
  document.addEventListener("click", async event => {
    const target = event.target.closest("button");
    if (!target || target.disabled) return;
    const d = target.dataset;
    if (!Object.keys(d).some(key => key.startsWith("pack") || key.startsWith("share"))) return;
    try {
      if (d.packBack === "catalog") return openCatalog({focusProject: selectedProject});
      if (d.packBack === "releases") return await showReleases(selectedProject);
      if ("packImport" in d) return await importPack();
      if ("packExport" in d) return await exportPack(d.packExport || active().id);
      if (d.packPage) return await search(Math.max(0, catalog.offset + (d.packPage === "next" ? 12 : -12)));
      if (d.packProject) return await showReleases(d.packProject);
      if ("packPrepare" in d) {
        const host = target.closest(".pack-release");
        const project = selectedProject;
        target.disabled = true;
        notice("Загружаем описание модпака…", false, true);
        const data = await act("packPreview", {project, version: choices.get(project)});
        notice("");
        if (!host?.isConnected || !document.querySelector("#dialog").open) {
          await act("packDiscard", {token: data.token});
          return;
        }
        previewPack(data, {project});
        return;
      }
      if ("packInstall" in d) {
        const data = {token: prepared.token, name: document.querySelector("[data-pack-name]").value, optional: [...document.querySelectorAll("[data-pack-optional]:checked")].map(input => input.value)};
        target.disabled = true;
        await runJob("packInstall", data);
        prepared = null;
        closeDialog();
        return;
      }
      if ("shareCreate" in d || "shareMakeInvite" in d) {
        target.disabled = true;
        const invitation = "shareMakeInvite" in d;
        const p = {id: active().id};
        if (invitation) {p.address = document.querySelector("[data-share-address]").value; p.label = active().name;}
        notice("Сверяем точные версии дополнений…", false, true);
        const data = await act(invitation ? "shareInvite" : "shareCreate", p);
        notice("");
        showShareResult(data);
        return;
      }
      if ("shareInvite" in d) {
        return modal("Приглашение в текущий мир", `<p>Открой одиночный мир для друзей Steam и скопируй зелёный адрес из чата Minecraft. В приглашение войдут адрес и точный состав выбранной сборки.</p><label>Адрес e4steam<input class="field" data-share-address placeholder="s-…-….steam" maxlength="150" spellcheck="false"></label><p class="note">После закрытия и повторного открытия мира нужен новый адрес. Доступ «Только по приглашению» также требует приглашения через Steam.</p><div class="dialog-actions">${button("data-share-make-invite", "Создать приглашение", "users", "main")}</div>`);
      }
      if ("shareImport" in d) return compareBox();
      if ("shareLoad" in d) return await loadShareFile();
      if ("shareCompare" in d) {
        target.disabled = true;
        comparisonView(await act("shareCompare", {id: active()?.id || "", descriptor: document.querySelector("[data-share-input]").value}));
        return;
      }
      if (d.shareApply) {
        target.disabled = true;
        await runJob("shareApply", {id: active()?.id || "", token: comparison.token, mode: d.shareApply});
        closeDialog();
        return;
      }
      if ("shareAddServer" in d) {
        const data = await act("shareAddServer", {id: active().id, address: comparison.invite.address, label: comparison.invite.label});
        notice(data.message);
        return;
      }
      if ("shareCopy" in d) {
        try {await navigator.clipboard.writeText(shared.code); notice("Код скопирован.");}
        catch {document.querySelector(".share-code").select(); notice("Код выделен. Нажми Ctrl+C.");}
        return;
      }
      if ("shareSave" in d) {
        const blob = new Blob([JSON.stringify(shared.descriptor, null, 2)], {type: "application/json"});
        const url = URL.createObjectURL(blob), link = document.createElement("a");
        link.href = url;
        link.download = shared.name;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        return;
      }
      if ("shareStatus" in d) {
        target.disabled = true;
        const data = await feature("shareRoomState", {id: active().id});
        const players = data.liveParticipants !== null ? ` Удалённых участников: ${data.peerCount}. ${data.liveParticipants.length ? data.liveParticipants.join(", ") + "." : ""}` : "";
        document.querySelector(".share-room-state").textContent = `${data.gameRunning ? "Minecraft запущен. " : "Minecraft сейчас закрыт. "}${data.message}${players} ${data.note}`;
      }
    } catch (error) {notice(error.message, true);}
    finally {if (target.isConnected) target.disabled = false;}
  });

  document.querySelector("#dialog").addEventListener("close", () => {
    // A native dialog's queued close event can arrive after another modal opens.
    if (document.querySelector("#dialog").open) return;
    rememberCatalog();
    stopSearch();
    releaseController?.abort();
    releaseController = null;
    discardPrepared();
    if (catalogOpen) {
      catalogOpen = false;
      const target = returnFocus?.isConnected ? returnFocus : document.querySelector('[data-act="packCatalog"]');
      target?.focus({preventScroll: true});
      returnFocus = null;
    }
  });

  window.PackHubUI = {
    render: renderPacks,
    openCatalog,
    mount() {loadArt(); if (document.querySelector(".pack-hub") && !catalog.page && !catalog.busy && !catalog.error) search();},
    exportButton(profile) {return profile ? button(`data-pack-export="${esc(profile.id)}"`, "Выгрузить .mrpack", "upload") : "";},
    preview: previewPack,
    async dispatch(action) {
      if (action === "packCatalog") {openCatalog(); return true;}
      if (action === "packImport") {await importPack(); return true;}
      if (action === "packExport") {await exportPack(active().id); return true;}
      return false;
    },
  };
  window.SharedBuildsUI = {panel: friendsPanel, mount() {}, async dispatch(action) {
    if (action === "shareCompare") {compareBox(); return true;}
    return false;
  }};
})();
