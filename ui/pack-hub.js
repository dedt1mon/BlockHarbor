"use strict";

// The catalog uses the existing modal host; filters, results and chosen releases
// stay independent of the build list and background refreshes.
(() => {
  const catalog = {q: "", version: "", loader: "", category: "", sort: "relevance", offset: 0, nextOffset: 0, page: null, error: "", busy: false, needsSearch: true};
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
    return `<section class="pack-hub"><div class="pack-intro"><p>Готовые сборки устанавливаются отдельно, со своей версией Minecraft и настройками.</p></div><form class="pack-search" data-pack-search role="search"><input type="search" class="field" name="q" maxlength="180" placeholder="Найти модпак" aria-label="Название модпака" value="${esc(catalog.q)}"><button class="btn main" type="submit">${ic("search")}Найти</button></form>
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
    if (!Object.keys(d).some(key => key.startsWith("pack"))) return;
    try {
      if (d.packBack === "catalog") return openCatalog({focusProject: selectedProject});
      if (d.packBack === "releases") return await showReleases(selectedProject);
      if (d.packPage) return await search(Math.max(0, catalog.offset + (d.packPage === "next" ? 12 : -12)));
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
    mount() {loadArt(); if (document.querySelector(".pack-hub") && !catalog.page && !catalog.busy && !catalog.error) search();},
    preview: previewPack,
    async dispatch(action) {
      if (action === "packCatalog") {openCatalog(); return true;}
      return false;
    },
  };

})();
