"use strict";

// The catalog uses the existing modal host; filters, results and chosen releases
// stay independent of the build list and background refreshes.
(() => {
  const catalog = {q: "", version: "", loader: "", category: "", sort: "relevance", offset: 0, nextOffset: 0, page: null, error: "", busy: false, needsSearch: true};
  const categories = {adventure: "Приключения", challenging: "Испытания", combat: "Сражения", "kitchen-sink": "Всё вместе", lightweight: "Лёгкие сборки", magic: "Магия", multiplayer: "Для друзей", optimization: "Оптимизация", quests: "Квесты", technology: "Технологии"};
  const community = {page: null, busy: false, error: "", needsSearch: true, controller: null};
  let pendingPublication = null, selectedCommunity = null;
  const exactCommunityId = () => /^BH-[0-9A-F]{12}$/i.test(catalog.q.trim());
  const choices = new Map();
  const releaseRows = new Map();
  let searchGeneration = 0, selectedProject = null, prepared = null;
  let previewSteamAccepted = new Set();
  let searchController = null, releaseController = null, catalogScroll = 0, returnFocus = null, catalogOpen = false;
  const feature = (action, params = {}, options = {}) => api("/api/feature?" + new URLSearchParams({action, ...params}), options);
  const button = (attr, label, icon = "", cls = "") => `<button type="button" class="btn ${cls}" ${attr}>${icon ? ic(icon) : ""}${label}</button>`;
  const option = (value, label, selected) => `<option value="${esc(value)}"${selected === value ? " selected" : ""}>${esc(label)}</option>`;
  const empty = (message) => `<div class="empty">${esc(message)}</div>`;
  const number = new Intl.NumberFormat("ru-RU");

  function discardPrepared() {
    previewSteamAccepted = new Set();
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
    community.controller?.abort();
    community.controller = null;
    community.busy = false;
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

  function openCatalog({focusProject = "", focusCommunity = ""} = {}) {
    discardPrepared();
    releaseController?.abort();
    releaseController = null;
    packModal("Каталог сборок", renderPacks(), "catalog");
    loadArt();
    const host = document.querySelector(".pack-hub");
    host.querySelector(".pack-results").scrollTop = catalogScroll;
    const previous = [...host.querySelectorAll("[data-pack-project], [data-pack-community]")].find(button =>
      button.dataset.packProject === focusProject || button.dataset.packCommunity === focusCommunity);
    (previous || host.querySelector('[name="q"]'))?.focus({preventScroll: true});
    if ((!catalog.page || catalog.needsSearch) && !catalog.busy) search(catalog.nextOffset);
    if (community.needsSearch && !community.busy) searchCommunity(community.page?.offset || 0);
  }

  function results() {
    if (!catalog.page) return "";
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

  function communityResults() {
    const page = community.page;
    let status = "";
    if (community.error) status = `<p class="pack-status pack-error" role="status">${esc(community.error)} ${button("data-pack-community-retry", "Повторить")}</p>`;
    else if (community.busy) status = '<p class="pack-status" role="status">Ищем сборки игроков…</p>';
    else if (page && !page.hits.length) status = '<p class="pack-status" role="status">Сборок игроков по этому запросу пока нет.</p>';
    const cards = (page?.hits || []).map(item => `<article class="pack-card"><h4>${esc(item.name)}</h4>
      <p>Minecraft ${esc(item.minecraft)} · ${esc(loaders[item.loader] || item.loader)} · ${esc(categories[item.category] || item.category)}</p>
      <div class="pack-card-footer"><span class="meta">${esc(item.id)} · ${fmt(item.size)}</span>${button(`data-pack-community="${esc(item.id)}"`, "Скачать", "download")}</div></article>`).join("");
    const navigation = page && page.total > page.limit ? `<div class="pack-pager">${button('data-pack-community-page="prev"' + (community.busy || !page.offset ? " disabled" : ""), "Назад", "back")}
      <span class="meta">${page.offset + 1}–${Math.min(page.total, page.offset + page.limit)} из ${number.format(page.total)}</span>
      ${button('data-pack-community-page="next"' + (community.busy || page.offset + page.limit >= page.total ? " disabled" : ""), "Далее", "arrow")}</div>` : "";
    return `<h3 class="pack-source-title">Сборки игроков BlockHarbor</h3>${status}<div class="pack-grid" aria-busy="${community.busy}">${cards}</div>${navigation}`;
  }

  async function searchCommunity(offset = 0) {
    community.controller?.abort();
    const controller = new AbortController();
    community.controller = controller;
    community.busy = true;
    community.needsSearch = true;
    community.error = "";
    const host = document.querySelector(".community-results");
    if (host) host.innerHTML = communityResults();
    try {
      const page = await feature("communitySearch", {q: catalog.q, category: catalog.category, version: catalog.version, loader: catalog.loader, offset}, {signal: controller.signal});
      if (controller.signal.aborted) return;
      community.page = page;
      community.needsSearch = false;
    } catch (error) {
      if (!controller.signal.aborted) community.error = error.message;
    } finally {
      if (community.controller === controller) {
        community.controller = null;
        community.busy = false;
        if (host?.isConnected) host.innerHTML = communityResults();
      }
    }
  }

  function searchCatalog() {
    community.page = null;
    catalog.page = null;
    updateResults();
    document.querySelector(".pack-results")?.scrollTo(0, 0);
    searchCommunity();
    search();
  }

  function publicationRows(list, expanded = false) {
    if (!list.length) return "";
    return `<details class="community-published"${expanded ? " open" : ""}><summary>Опубликованные сборки · ${list.length}</summary>
      ${list.map(row => `<section class="community-receipt"><strong>${esc(row.name)}</strong><div class="community-id"><input class="field" readonly aria-label="ID сборки ${esc(row.name)}" value="${esc(row.build)}">
        ${button("data-pack-copy-id", "Скопировать ID", "copy")}</div>
        ${button(`data-pack-withdraw="${esc(row.build)}"`, "Снять с публикации", "trash")}
        <div class="community-confirm" hidden><p>Убрать эту сборку из общего каталога? Её ID перестанет работать. Скачанные копии останутся у игроков.</p>
        ${button("data-pack-keep", "Отмена")}${button(`data-pack-withdraw-confirm="${esc(row.build)}"`, "Снять с публикации", "", "remove-confirm")}</div></section>`).join("")}</details>`;
  }

  async function readPublications(form, expanded) {
    const host = form.querySelector(".community-receipts");
    const revision = String(Number(host.dataset.revision || 0) + 1);
    host.dataset.revision = revision;
    try {
      const data = await feature("communityPublication", {id: form.dataset.packPublish});
      if (form.isConnected && host.dataset.revision === revision) host.innerHTML = publicationRows(data.publications, expanded);
    } catch (error) {
      if (form.isConnected && host.dataset.revision === revision) host.textContent = error.message;
    }
  }

  function showPublish(id) {
    const profile = state.profiles.find(p => p.id === id);
    if (!profile) return;
    modal("Поделиться сборкой", `<form class="community-publish" data-pack-publish="${esc(id)}">
      <label>Название в каталоге<input class="field" name="name" value="${esc(profile.name)}" required maxlength="48"></label>
      <div class="field-group"><label>Версия Minecraft<input class="field" readonly value="${esc(profile.version)}"></label>
      <label>Загрузчик<input class="field" readonly value="${esc(loaders[profile.loader] || profile.loader)}"></label></div>
      <label>Категория<select class="field" name="category" aria-label="Категория" required><option value="">Выбери категорию</option>${Object.entries(categories).map(([value, label]) => option(value, label, "")).join("")}</select></label>
      <label class="community-config-toggle"><input type="checkbox" name="includeConfigs" aria-describedby="communityConfigHelp"> Включить настройки модов</label>
      <p class="note" id="communityConfigHelp" hidden>В архив войдут подходящие файлы из config и defaultconfigs. Перед публикацией проверь их содержимое на личные данные.</p>
      <p class="note">Сборка станет общедоступной. Игроки смогут найти её по названию или ID и скачать моды, ресурс-паки и шейдеры. Выбор ресурс-паков сохранится в сборке. Миры, журналы и данные учётных записей останутся на этом компьютере.</p>
      <p role="status" class="community-feedback"></p><div class="dialog-actions">${button('data-act="close"', "Закрыть")}<button class="btn main" type="submit">${ic("share")}Опубликовать</button></div>
      <div class="community-receipts"></div></form>`);
    const form = document.querySelector("[data-pack-publish]");
    if (pendingPublication?.id === id) setPublicationBusy(form, true, pendingPublication.progress);
    readPublications(form, false);
  }

  function setPublicationBusy(form, busy, message) {
    form.setAttribute("aria-busy", String(busy));
    for (const control of form.querySelectorAll('input[name], select, [type="submit"], [data-pack-withdraw-confirm]')) control.disabled = busy;
    form.querySelector(".community-feedback").textContent = message;
  }

  function observeJob(job) {
    const request = pendingPublication;
    if (!request || !request.job || job.id !== request.job || job.busy) return;
    pendingPublication = null;
    if (job.ok) {
      community.needsSearch = true;
      if (document.querySelector(".community-results")) searchCommunity();
    }
    const form = document.querySelector("[data-pack-publish]");
    if (form?.dataset.packPublish !== request.id) return;
    setPublicationBusy(form, false, job.ok ? request.done : job.message);
    if (job.ok) readPublications(form, true);
  }

  async function publicationAction(form, action, params) {
    if (pendingPublication) return notice("Дождись завершения текущей публикации.", true);
    const publishing = action === "communityPublish";
    const request = {id: form.dataset.packPublish, job: null,
      progress: publishing ? "Публикуем сборку…" : "Снимаем сборку с публикации…",
      done: publishing ? "Сборка опубликована. Её ID можно скопировать ниже." : "Сборка снята с публикации."};
    pendingPublication = request;
    setPublicationBusy(form, true, request.progress);
    try {
      request.job = await runJob(action, {id: request.id, ...params});
      if (state.job) observeJob(state.job);
    } catch (error) {
      pendingPublication = null;
      if (form.isConnected) setPublicationBusy(form, false, error.message);
      else notice(error.message, true);
    }
  }

  function showCommunityBuild(id) {
    const item = community.page?.hits.find(hit => hit.id === id);
    if (!item) return;
    selectedCommunity = id;
    selectedProject = null;
    rememberCatalog();
    stopSearch();
    packModal("Скачать сборку игрока", `${button('data-pack-back="catalog"', "К каталогу", "back", "pack-back")}
      <section class="pack-preview"><h3>${esc(item.name)}</h3><p class="meta">${esc(item.id)}</p><div class="tags">
      <span class="pill">Minecraft ${esc(item.minecraft)}</span><span class="pill">${esc(loaders[item.loader] || item.loader)}</span>
      <span class="pill">${esc(categories[item.category] || item.category)}</span><span class="pill">${fmt(item.size)}</span></div>
      <p>Сборка будет установлена отдельно. Моды могут выполнять код при запуске Minecraft. Устанавливай сборки авторов, которым доверяешь.</p>
      <div class="dialog-actions">${button(`data-pack-community-install="${esc(id)}"`, "Установить отдельную сборку", "download", "main")}</div></section>`, "community");
  }

  function renderPacks() {
    return `<section class="pack-hub"><div class="pack-intro"><p>Готовые сборки устанавливаются отдельно, со своей версией Minecraft и настройками.</p></div><form class="pack-search" data-pack-search role="search"><input type="search" class="field" name="q" maxlength="180" placeholder="Название сборки или ID" aria-label="Название сборки или ID" value="${esc(catalog.q)}"><button class="btn main" type="submit">${ic("search")}Найти</button></form>
      <div class="pack-filters"><label>Minecraft<select class="field" data-pack-filter="version">${option("", "Все версии", catalog.version)}${(state.versions || []).map(v => option(v, v, catalog.version)).join("")}</select></label>
      <label>Загрузчик<select class="field" data-pack-filter="loader">${option("", "Любой", catalog.loader)}${Object.entries(loaders).map(([v, label]) => option(v, label, catalog.loader)).join("")}</select></label>
      <label>Категория<select class="field" data-pack-filter="category">${Object.entries({"": "Все категории", ...categories}).map(([v, label]) => option(v, label, catalog.category)).join("")}</select></label>
      <label>Сортировка Modrinth<select class="field" data-pack-filter="sort">${Object.entries({relevance: "По релевантности", downloads: "По загрузкам", follows: "По подписчикам", newest: "Сначала новые", updated: "Недавно обновлённые"}).map(([v, label]) => option(v, label, catalog.sort)).join("")}</select></label></div>
      <div class="pack-results"><section class="community-results" aria-label="Сборки игроков BlockHarbor">${communityResults()}</section>
      <section class="pack-modrinth" aria-label="Модпаки Modrinth"${exactCommunityId() ? " hidden" : ""}><h3 class="pack-source-title">Модпаки Modrinth</h3><p class="pack-status${catalog.error ? " pack-error" : ""}" role="status">${esc(catalog.error || (catalog.busy ? "Загружаем каталог…" : ""))}</p><div class="pack-source-results" aria-busy="${catalog.busy}">${results()}</div><div class="pack-page-host">${pager()}</div></section></div></section>`;
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
    host.querySelector(".pack-modrinth").hidden = exactCommunityId();
    host.querySelector('[data-pack-filter="sort"]').disabled = exactCommunityId();
    host.querySelector(".pack-source-results").innerHTML = results();
    host.querySelector(".pack-source-results").setAttribute("aria-busy", String(catalog.busy));
    host.querySelector(".pack-page-host").innerHTML = pager();
    const status = host.querySelector(".pack-modrinth .pack-status");
    status.textContent = catalog.error || (catalog.busy ? "Загружаем каталог…" : "");
    status.classList.toggle("pack-error", !!catalog.error);
    loadArt();
  }

  async function search(offset = 0) {
    const generation = ++searchGeneration;
    searchController?.abort();
    if (exactCommunityId()) {
      searchController = null;
      catalog.busy = false;
      catalog.needsSearch = false;
      updateResults();
      return;
    }
    const controller = new AbortController();
    searchController = controller;
    catalog.nextOffset = offset;
    catalog.needsSearch = true;
    catalog.busy = true;
    catalog.error = "";
    // Keep the previous page visible until a replacement has arrived.
    const host = document.querySelector(".pack-hub");
    if (host) {
      host.querySelector(".pack-source-results").setAttribute("aria-busy", "true");
      host.querySelector(".pack-modrinth .pack-status").textContent = "Загружаем каталог…";
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
    selectedCommunity = null;
    packModal("Выпуски модпака", `${button('data-pack-back="catalog"', "К каталогу", "back", "pack-back")}<div class="pack-release" role="status">Получаем версии…</div>`, "releases");
    const host = document.querySelector(".pack-release");
    try {
      const data = await feature("packVersions", {project}, {signal: controller.signal});
      if (!host.isConnected || controller.signal.aborted || selectedProject !== project) return;
      releaseRows.set(project, data.versions);
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

  function previewPack(data, {project = null, acceptedPairs = new Set()} = {}) {
    if (prepared && prepared.token !== data.token) discardPrepared();
    prepared = data;
    previewSteamAccepted = acceptedPairs;
    selectedProject = project;
    packModal("Установить модпак", `${button(`data-pack-back="${project ? "releases" : "catalog"}"`, project ? "К выпускам" : "К каталогу", "back", "pack-back")}<section class="pack-preview"><h3>${esc(data.title)}</h3><p>${esc(data.summary)}</p><div class="tags"><span class="pill">Minecraft ${esc(data.minecraft)}</span><span class="pill">${esc(loaders[data.loader] || data.loader)} ${esc(data.loaderVersion || "")}</span><span class="pill">${data.files} файлов</span><span class="pill">${fmt(data.downloadBytes)} загрузка</span></div>
      <label>Название новой сборки<input class="field" data-pack-name maxlength="48" value="${esc(data.name)}"></label>
      ${data.optional.length ? `<h4>Необязательные компоненты</h4><div class="pack-optional">${data.optional.map(row => `<label><input type="checkbox" data-pack-optional value="${esc(row.path)}"><span>${esc(row.path)}</span><small>${fmt(row.size)}</small></label>`).join("")}</div>` : ""}
      ${data.unsupported ? `<p class="note">${data.unsupported} файлов только для выделенного сервера будут пропущены.</p>` : ""}
      <p class="note">Текущие сборки останутся на месте. Файлы проверяются по размеру, SHA-1 и SHA-512.</p><div class="dialog-actions">${button("data-pack-install", "Установить отдельную сборку", "download", "main")}</div></section>`, "preview");
  }

  document.addEventListener("submit", event => {
    const form = event.target;
    if (form.matches("[data-pack-publish]")) {
      event.preventDefault();
      const data = new FormData(form);
      const name = data.get("name").trim();
      if (!name) {form.elements.name.focus(); return notice("Введи название сборки.", true);}
      return void publicationAction(form, "communityPublish", {name, category: data.get("category"), includeConfigs: data.has("includeConfigs")});
    }
    if (!form.matches("[data-pack-search]")) return;
    event.preventDefault();
    catalog.q = new FormData(event.target).get("q") || "";
    searchCatalog();
  });
  document.addEventListener("change", event => {
    if (event.target.matches('.community-publish [name="includeConfigs"]')) {
      event.target.form.querySelector("#communityConfigHelp").hidden = !event.target.checked;
      return;
    }
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
    if (!Object.keys(d).some(key => key.startsWith("pack"))) return;
    try {
      if (d.packShare) return showPublish(d.packShare);
      if ("packCopyId" in d) {
        const input = target.closest(".community-id").querySelector("input");
        input.focus(); input.select();
        try {await navigator.clipboard.writeText(input.value); notice("ID сборки скопирован.");}
        catch {notice("ID выделен. Нажми Ctrl+C, чтобы скопировать.");}
        return;
      }
      if (d.packWithdraw || "packKeep" in d) {
        const row = target.closest(".community-receipt");
        row.querySelector(".community-confirm").hidden = !d.packWithdraw;
        row.querySelector("[data-pack-withdraw]").hidden = !!d.packWithdraw;
        row.querySelector(d.packWithdraw ? "[data-pack-keep]" : "[data-pack-withdraw]").focus();
        return;
      }
      if (d.packWithdrawConfirm) return await publicationAction(target.closest("form"), "communityUnpublish", {build: d.packWithdrawConfirm});
      if (d.packCommunity) return showCommunityBuild(d.packCommunity);
      if (d.packCommunityInstall) {
        const item = community.page?.hits.find(hit => hit.id === d.packCommunityInstall);
        if (!item) return;
        target.disabled = true;
        const confirmation = await confirmSteamCompatibility({version: item.minecraft, loader: item.loader}, {isCurrent: () => target.isConnected && document.querySelector("#dialog").open});
        if (!confirmation) return;
        await runJob("communityInstall", {build: item.id, allowWithoutSteam: confirmation.allowWithoutSteam});
        if (target.isConnected) closeDialog();
        return;
      }
      if ("packCommunityRetry" in d) return await searchCommunity(community.page?.offset || 0);
      if (d.packCommunityPage) {
        const page = community.page;
        if (page) await searchCommunity(Math.max(0, page.offset + (d.packCommunityPage === "next" ? page.limit : -page.limit)));
        document.querySelector(".community-results")?.scrollIntoView({block: "start"});
        return;
      }
      if (d.packBack === "catalog") return openCatalog({focusProject: selectedProject, focusCommunity: selectedCommunity});
      if (d.packBack === "releases") return await showReleases(selectedProject);
      if (d.packPage) {
        await search(Math.max(0, catalog.offset + (d.packPage === "next" ? 12 : -12)));
        document.querySelector(".pack-modrinth")?.scrollIntoView({block: "start"});
        return;
      }
      if (d.packProject) return await showReleases(d.packProject);
      if ("packPrepare" in d) {
        const host = target.closest(".pack-release");
        const project = selectedProject;
        const version = choices.get(project);
        const release = releaseRows.get(project)?.find(row => row.id === version);
        if (!release) throw new Error("Выбери выпуск модпака ещё раз.");
        const isCurrent = () => host?.isConnected && document.querySelector("#dialog").open && selectedProject === project && choices.get(project) === version;
        target.disabled = true;
        const confirmation = await confirmSteamCompatibility({}, {report: release.steam || {supported: false, unknown: true, variants: []}, isCurrent});
        if (!confirmation) return;
        notice("Загружаем описание модпака…", false, true);
        const data = await act("packPreview", {project, version});
        notice("");
        if (!isCurrent()) {
          await act("packDiscard", {token: data.token});
          return;
        }
        previewPack(data, {project, acceptedPairs: confirmation.acceptedPairs});
        return;
      }
      if ("packInstall" in d) {
        const current = prepared;
        if (!current) return;
        const host = target.closest(".pack-preview");
        const isCurrent = () => prepared === current && host?.isConnected && document.querySelector("#dialog").open;
        target.disabled = true;
        const confirmation = await confirmSteamCompatibility({version: current.minecraft, loader: current.loader}, {report: current.steam, acceptedPairs: previewSteamAccepted, isCurrent});
        if (!confirmation) return;
        const data = {token: current.token, name: host.querySelector("[data-pack-name]").value, optional: [...host.querySelectorAll("[data-pack-optional]:checked")].map(input => input.value), allowWithoutSteam: confirmation.allowWithoutSteam};
        await runJob("packInstall", data);
        prepared = null;
        previewSteamAccepted = new Set();
        closeDialog();
        return;
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
    observeJob,
    mount() {
      loadArt();
      if (!document.querySelector(".pack-hub")) return;
      if (!catalog.page && !catalog.busy && !catalog.error) search();
      if (community.needsSearch && !community.busy && !community.error) searchCommunity();
    },
    preview: previewPack,
    async dispatch(action) {
      if (action === "packCatalog") {openCatalog(); return true;}
      return false;
    },
  };

})();
